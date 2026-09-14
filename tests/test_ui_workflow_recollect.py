"""워크플로우 카드의 원문 다시 수집 (2026-09-14).

실사이트에 나가지 않는다. 지금 1회 실행 테스트와 같은 python.org 픽스처 스텁을 쓰고, 백그라운드로
보내진 작업은 테스트가 직접 돌린다 (`tests/test_ui_workflow_run.py`). 모델도 부르지 않는다 — 분류
키를 비워 다시 분류가 키 없음으로 바로 끝나게 한다.

| 확인 | 깨지면 |
|---|---|
| 누르면 바로 시작하지 않고 건수부터 보여 준다 | AI 비용이 나가는 조작을 모른 채 누른다 |
| 시작하면 바로 돌아오고 카드가 폴링한다 | 한 시간 걸리는 작업에서 브라우저 요청이 먼저 끊긴다 |
| 끝나면 갈아 끼운 건수와 다시 분류 결과를 적는다 | 무엇이 바뀌었는지 모른다 |
| 실행 중이면 시작하지 않는다 | 같은 사이트를 두 작업이 동시에 때린다 |
| 도는 동안 카드가 진행 상황을 적는다 | 한 시간 동안 멈춘 것과 도는 것을 가를 수 없다 |
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Coroutine, Iterator
from typing import Any

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi.testclient import TestClient

from app import db
from app.api import crawlers as crawlers_api
from app.api import ui_workflows
from app.api import workflows as workflows_api
from app.config import get_settings
from app.crawler import recollect
from app.main import app
from app.scheduler import RunGate, WorkflowScheduler
from tests.test_ui_workflow_run import add_workflow, run_background, sent_to, stub_fetcher


@pytest.fixture
def conn(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    # 다시 분류가 모델을 부르지 않게 키를 비운다. 키 없음으로 바로 끝난다
    for name in ("GEMINI_API_KEY", "CLAUDE_API_KEY", "GPT_API_KEY", "QWEN_API_KEY"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("CLASSIFY_PROVIDER", "gemini")
    get_settings.cache_clear()
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    try:
        yield connection
    finally:
        connection.close()
        get_settings.cache_clear()


@pytest.fixture
def scheduler() -> Iterator[WorkflowScheduler]:
    async def do_nothing(workflow_id: int) -> None:
        return None

    instance = WorkflowScheduler(scheduler=AsyncIOScheduler(timezone="UTC"), runner=do_nothing)
    try:
        yield instance
    finally:
        instance.shutdown()


@pytest.fixture
def started() -> Iterator[list[Coroutine[Any, Any, None]]]:
    sent: list[Coroutine[Any, Any, None]] = []
    try:
        yield sent
    finally:
        for coro in sent:
            coro.close()


@pytest.fixture
def client(
    tmp_path: pathlib.Path,
    conn: sqlite3.Connection,
    scheduler: WorkflowScheduler,
    started: list[Coroutine[Any, Any, None]],
) -> Iterator[TestClient]:
    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(tmp_path / "jobs.db")
        try:
            yield connection
        finally:
            connection.close()

    fetcher = stub_fetcher()
    gate = RunGate(lambda: 2)
    app.dependency_overrides[workflows_api.get_connection] = request_connection
    app.dependency_overrides[crawlers_api.get_connection] = request_connection
    app.dependency_overrides[crawlers_api.get_crawl_fetcher] = lambda: fetcher
    app.dependency_overrides[workflows_api.get_workflow_scheduler] = lambda: scheduler
    app.dependency_overrides[ui_workflows.get_run_gate] = lambda: gate
    app.dependency_overrides[ui_workflows.get_run_launcher] = lambda: sent_to(started)
    app.dependency_overrides[ui_workflows.get_run_connect] = lambda: (
        lambda: db.connect(tmp_path / "jobs.db")
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        ui_workflows._running.clear()
        recollect.PROGRESS.clear()


def collected_once(
    client: TestClient, started: list[Coroutine[Any, Any, None]], workflow_id: int
) -> None:
    """지금 1회 실행으로 공고를 담아 둔다."""
    client.post(f"/ui/workflows/{workflow_id}/run")
    run_background(started)


def test_누르면_바로_시작하지_않고_건수부터_보여_준다(
    client: TestClient, conn: sqlite3.Connection, started: list[Coroutine[Any, Any, None]]
) -> None:
    workflow_id = add_workflow(conn)
    collected_once(client, started, workflow_id)
    total = conn.execute(
        "SELECT count(DISTINCT source_url) FROM raw_jobs WHERE workflow_id = ?", (workflow_id,)
    ).fetchone()[0]

    body = client.get(f"/ui/workflows/{workflow_id}/recollect").text

    assert "원문 다시 수집 — 시작 전 확인" in body
    assert f"이 워크플로우 공고 {total}건 중" in body
    assert f'hx-post="/ui/workflows/{workflow_id}/recollect"' in body
    assert started == []


def test_시작하면_바로_돌아오고_끝나면_갈아_끼운_건수와_다시_분류_결과를_적는다(
    client: TestClient, conn: sqlite3.Connection, started: list[Coroutine[Any, Any, None]]
) -> None:
    workflow_id = add_workflow(conn)
    collected_once(client, started, workflow_id)
    job = conn.execute(
        "SELECT id, raw_data_json FROM raw_jobs WHERE workflow_id = ? ORDER BY id LIMIT 1",
        (workflow_id,),
    ).fetchone()
    stale = {**json.loads(job["raw_data_json"]), "body": "옛 본문"}
    conn.execute(
        "UPDATE raw_jobs SET raw_data_json = ? WHERE id = ?",
        (json.dumps(stale, ensure_ascii=False), job["id"]),
    )

    body = client.post(f"/ui/workflows/{workflow_id}/recollect").text

    assert ui_workflows.RECOLLECT_STARTED in body
    assert 'hx-trigger="every 2s"' in body
    assert len(started) == 1

    run_background(started)

    run = conn.execute(
        "SELECT id, trigger, status FROM crawl_runs WHERE workflow_id = ? ORDER BY id DESC LIMIT 1",
        (workflow_id,),
    ).fetchone()
    assert (run["trigger"], run["status"]) == ("recollect", "success")
    history = conn.execute(
        "SELECT raw_job_id, raw_data_json FROM raw_job_history WHERE run_id = ?", (run["id"],)
    ).fetchall()
    assert [row["raw_job_id"] for row in history] == [job["id"]]
    assert json.loads(history[0]["raw_data_json"])["body"] == "옛 본문"

    card = client.get(f"/ui/workflows/{workflow_id}/card?polled=true").text
    assert f"원문 다시 수집 {run['id']} 이 성공으로 끝났다" in card
    assert "바뀜 1건" in card
    assert "다시 분류:" in card
    assert "원문 다시 수집" in card


def test_실행_중이면_시작하지_않는다(
    client: TestClient, conn: sqlite3.Connection, started: list[Coroutine[Any, Any, None]]
) -> None:
    workflow_id = add_workflow(conn)
    collected_once(client, started, workflow_id)
    ui_workflows._running.add(workflow_id)

    body = client.post(f"/ui/workflows/{workflow_id}/recollect").text

    assert "이미 실행 중이다" in body
    assert started == []


def test_담은_공고가_없으면_시작하지_않는다(
    client: TestClient, conn: sqlite3.Connection, started: list[Coroutine[Any, Any, None]]
) -> None:
    workflow_id = add_workflow(conn)

    body = client.post(f"/ui/workflows/{workflow_id}/recollect").text

    assert "다시 수집할 것이 없다" in body
    assert started == []


def test_도는_동안_카드가_진행_상황을_적는다(client: TestClient, conn: sqlite3.Connection) -> None:
    workflow_id = add_workflow(conn)
    ui_workflows._running.add(workflow_id)
    recollect.PROGRESS[workflow_id] = recollect.RecollectProgress(
        targets=5, done=2, changed=1, unchanged=1
    )

    body = client.get(f"/ui/workflows/{workflow_id}/card").text

    assert "원문을 다시 수집하는 중이다 — 2/5건" in body
    assert 'hx-trigger="every 2s"' in body
