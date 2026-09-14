"""화면에서의 지금 1회 실행 테스트 (15.3, 16.4).

실사이트에 나가지 않는다. fetch 클라이언트를 저장된 python.org 픽스처를 돌려주는 스텁으로
갈아끼우고, 확인하는 것은 `crawl_runs` 행과 `raw_jobs` 적재, 그리고 갈린 조각이다.

16.4 부터 응답은 실행을 기다리지 않는다. 라우트는 시작만 하고 돌아오고, 실제 실행은 백그라운드
작업이 끝까지 간다. 테스트는 그 작업을 보내는 자리(`get_run_launcher`)를 갈아끼워 코루틴을
받아 두고, 원하는 시점에 직접 돌린다 — 백그라운드가 언제 끝나는지를 기다리는 테스트는 느리고
시점에 따라 다르게 실패한다.

| 확인 | 깨지면 |
|---|---|
| 응답이 실행을 기다리지 않는다 | 2분 걸리는 실행에서 브라우저 클릭이 먼저 끊긴다 |
| 실행 중인 카드가 폴링을 달고 온다 | 시작만 하고 결과가 화면에 영영 안 온다 |
| 끝난 카드에는 폴링이 없다 | 다 끝난 워크플로우를 브라우저가 2초마다 계속 물어본다 |
| 실행이 `crawl_runs` 와 `raw_jobs` 를 남긴다 | 화면이 실행했다고 적고 아무것도 안 남는다 |
| 갈리는 것은 누른 워크플로우 하나뿐이다 | 다른 워크플로우에 입력하던 주기가 같이 날아간다 |
| 진행 중이면 새로 시작하지 않는다 | 같은 워크플로우의 실행 둘이 같은 사이트를 동시에 때린다 |
| 상한이 차 있으면 시작하지 않는다 | 상한이 화면 경로에서만 무시된다 |
| 실패는 사유까지 적는다 | 실패가 조용히 지나가 무엇을 고쳐야 하는지 알 수 없다 |
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import re
import sqlite3
from collections.abc import Coroutine, Iterator
from typing import Any

import httpx
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import Request
from fastapi.testclient import TestClient

from app import db
from app.api import crawlers as crawlers_api
from app.api import ui_workflows
from app.api import workflows as workflows_api
from app.config import Settings
from app.crawler.fetcher import Fetcher
from app.main import app
from app.scheduler import RunGate, WorkflowScheduler

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
LIST_HTML = (FIXTURES / "pythonorg-jobs-list-20260821.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "pythonorg-job-detail-20260821.html").read_text(encoding="utf-8")

LIST_URL = "https://www.python.org/jobs/"
ROBOTS = "User-agent: *\nDisallow:\n"

SELECTORS: dict[str, Any] = {
    "list": {
        "item": "ol.list-recent-jobs > li",
        "title": "span.listing-company-name > a",
        "link": "span.listing-company-name > a",
        "date": "span.listing-posted time",
    },
    "detail": {
        "title": "h1.listing-company span.company-name",
        "body": "div.job-description",
        "qualifications": "",
        "recruitment_end_at": "",
        "department": "span.listing-company-category a",
    },
}

MISSING_SELECTORS: dict[str, Any] = {
    "list": {"item": "ol.nothing-here > li", "title": "a", "link": "a", "date": "time"},
    "detail": {
        "title": "h1",
        "body": "div",
        "qualifications": "",
        "recruitment_end_at": "",
        "department": "",
    },
}


def stub_fetcher() -> Fetcher:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        if request.url.path == "/jobs/":
            return httpx.Response(200, text=LIST_HTML)
        return httpx.Response(200, text=DETAIL_HTML)

    async def no_wait(seconds: float) -> None:
        return None

    return Fetcher(
        settings=Settings(crawl_delay_seconds=0.0, crawl_max_retries=0),
        transport=httpx.MockTransport(handle),
        sleep=no_wait,
    )


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    try:
        yield connection
    finally:
        connection.close()


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
def gate() -> RunGate:
    """상한 2 짜리 문. 전역 문을 쓰면 상한을 운영 DB 에서 읽는다."""
    return RunGate(lambda: 2)


@pytest.fixture
def started() -> Iterator[list[Coroutine[Any, Any, None]]]:
    """백그라운드로 보내진 실행. 테스트가 직접 돌린다."""
    sent: list[Coroutine[Any, Any, None]] = []
    try:
        yield sent
    finally:
        # 돌리지 않고 끝난 코루틴을 닫는다. 안 닫으면 경고만 남고 조용히 사라진다
        for coro in sent:
            coro.close()


@pytest.fixture
def client(
    tmp_path: pathlib.Path,
    conn: sqlite3.Connection,
    scheduler: WorkflowScheduler,
    gate: RunGate,
    started: list[Coroutine[Any, Any, None]],
) -> Iterator[TestClient]:
    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(tmp_path / "jobs.db")
        try:
            yield connection
        finally:
            connection.close()

    fetcher = stub_fetcher()
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


def sent_to(
    sent: list[Coroutine[Any, Any, None]],
) -> Any:
    def launch(coro: Coroutine[Any, Any, None]) -> None:
        sent.append(coro)

    return launch


def run_background(sent: list[Coroutine[Any, Any, None]]) -> None:
    """백그라운드가 하는 일을 그대로, 이 자리에서 끝까지 돌린다."""
    while sent:
        asyncio.run(sent.pop(0))


def add_workflow(
    conn: sqlite3.Connection,
    name: str = "python.org 채용",
    selectors: dict[str, Any] | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO crawlers (name, list_url, detail_url, selectors_json, status)
        VALUES (?, ?, ?, ?, 'promoted')
        """,
        (
            name,
            LIST_URL,
            "https://www.python.org/jobs/8126/",
            json.dumps(SELECTORS if selectors is None else selectors),
        ),
    )
    crawler_id = int(cursor.lastrowid or 0)
    cursor = conn.execute(
        """
        INSERT INTO workflows (crawler_id, name, interval_minutes, status)
        VALUES (?, ?, 360, 'active')
        """,
        (crawler_id, name),
    )
    return int(cursor.lastrowid or 0)


def rows_in(html: str) -> list[str]:
    return re.findall(r'<article id="workflow-row-(\d+)"', html)


def counts(conn: sqlite3.Connection, workflow_id: int) -> tuple[int, int]:
    runs = conn.execute(
        "SELECT COUNT(*) AS n FROM crawl_runs WHERE workflow_id = ?", (workflow_id,)
    ).fetchone()["n"]
    jobs = conn.execute(
        "SELECT COUNT(*) AS n FROM raw_jobs WHERE workflow_id = ?", (workflow_id,)
    ).fetchone()["n"]
    return int(runs), int(jobs)


def test_1회_실행은_시작만_하고_바로_돌아온다(
    client: TestClient, conn: sqlite3.Connection, started: list[Coroutine[Any, Any, None]]
) -> None:
    """응답이 실행을 기다리면 2분짜리 실행에서 브라우저 클릭이 먼저 끊긴다 (16.4)."""
    workflow_id = add_workflow(conn)

    html = client.post(f"/ui/workflows/{workflow_id}/run").text

    # 돌아온 시점에는 아무것도 실행되지 않았다. 시작만 보냈다
    assert len(started) == 1
    assert counts(conn, workflow_id) == (0, 0)
    # 그 자리에는 진행 중이라고 적혀 있고, 카드가 스스로 갱신할 준비가 돼 있다
    assert "수집 진행 중" in html
    assert 'hx-trigger="every 2s"' in html
    assert f'hx-get="/ui/workflows/{workflow_id}/card?polled=true"' in html


def test_1회_실행이_crawl_runs_와_raw_jobs_를_남긴다(
    client: TestClient, conn: sqlite3.Connection, started: list[Coroutine[Any, Any, None]]
) -> None:
    workflow_id = add_workflow(conn)

    client.post(f"/ui/workflows/{workflow_id}/run")
    run_background(started)

    runs, jobs = counts(conn, workflow_id)
    assert runs == 1
    assert jobs > 0

    run = conn.execute("SELECT * FROM crawl_runs WHERE workflow_id = ?", (workflow_id,)).fetchone()
    assert run["status"] == "success"
    assert run["finished_at"] is not None

    # 끝난 뒤 카드가 물어보면 결과가 나온다
    html = client.get(f"/ui/workflows/{workflow_id}/card?polled=true").text
    assert f"실행 {run['id']} 이 성공으로 끝났다" in html
    assert f"신규 {jobs}건" in html


def test_실행이_끝나면_폴링이_멈춘다(
    client: TestClient, conn: sqlite3.Connection, started: list[Coroutine[Any, Any, None]]
) -> None:
    """멈추는 판단은 서버가 한다. 브라우저는 끝났는지 알 방법이 없다 (16.4)."""
    workflow_id = add_workflow(conn)
    client.post(f"/ui/workflows/{workflow_id}/run")

    # 도는 동안에는 계속 물어본다
    during = client.get(f"/ui/workflows/{workflow_id}/card?polled=true").text
    assert 'hx-trigger="every 2s"' in during
    assert "수집 진행 중" in during
    # 조작은 잠겨 있다. 2초마다 갈리는 카드에서 주기를 입력하면 그때마다 날아간다
    assert re.search(r"<button[^>]*\sdisabled", during) is not None
    assert re.search(r"<input[^>]*\sdisabled", during) is not None

    run_background(started)

    after = client.get(f"/ui/workflows/{workflow_id}/card?polled=true").text
    assert "hx-trigger" not in after
    assert "수집 진행 중" not in after
    assert re.search(r"<button[^>]*\sdisabled", after) is None  # 조작이 풀린다
    assert '<span class="state-ok">성공</span>' in after


def test_실행_뒤에_그_카드의_최근_실행과_카운트가_갱신된다(
    client: TestClient, conn: sqlite3.Connection, started: list[Coroutine[Any, Any, None]]
) -> None:
    workflow_id = add_workflow(conn)
    other = add_workflow(conn, name="다른 워크플로우")

    posted = client.post(f"/ui/workflows/{workflow_id}/run").text
    run_background(started)
    html = client.get(f"/ui/workflows/{workflow_id}/card?polled=true").text

    # 갈리는 것은 누른 워크플로우 하나뿐이다. 시작할 때도, 끝났을 때도
    assert rows_in(posted) == [str(workflow_id)]
    assert rows_in(html) == [str(workflow_id)]
    assert str(other) not in rows_in(html)

    row = conn.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
    assert (row["success_count"], row["fail_count"]) == (1, 0)
    assert row["last_run_at"] is not None
    # 최근 실행과 최근 결과가 이 조각에서 이미 갱신돼 있다. 새로고침을 눌러야 보이면 안 된다
    assert "실행 기록 없음" not in html
    assert '<span class="state-ok">성공</span>' in html
    assert counts(conn, other) == (0, 0)


def test_이미_실행_중이면_새로_시작하지_않는다(
    client: TestClient, conn: sqlite3.Connection, started: list[Coroutine[Any, Any, None]]
) -> None:
    """스케줄러의 tick 스킵과 같은 판단이다. 끝나지 않은 실행은 `status` 가 비어 있다."""
    workflow_id = add_workflow(conn)
    conn.execute("INSERT INTO crawl_runs (workflow_id) VALUES (?)", (workflow_id,))

    html = client.post(f"/ui/workflows/{workflow_id}/run").text

    assert "이미 실행 중이다" in html
    assert started == []  # 시작을 보내지도 않았다
    runs, jobs = counts(conn, workflow_id)
    assert runs == 1  # 심어 둔 그 행 하나뿐이다
    assert jobs == 0
    # 그래도 끝나면 갱신되게 폴링은 붙여 보낸다. 스케줄러가 돌리는 중일 수 있다
    assert 'hx-trigger="every 2s"' in html


def test_오래된_미완_행은_진행_중으로_보지_않는다(
    client: TestClient, conn: sqlite3.Connection, started: list[Coroutine[Any, Any, None]]
) -> None:
    """프로세스가 죽으며 남긴 행이 1회 실행을 영영 막고 카드를 영영 폴링시키면 안 된다."""
    workflow_id = add_workflow(conn)
    conn.execute(
        "INSERT INTO crawl_runs (workflow_id, started_at) VALUES (?, datetime('now', '-1 day'))",
        (workflow_id,),
    )

    html = client.post(f"/ui/workflows/{workflow_id}/run").text

    assert "이미 실행 중이다" not in html
    assert len(started) == 1


async def test_상한이_차_있으면_기다리지_않고_건너뛴다(
    conn: sqlite3.Connection, scheduler: WorkflowScheduler
) -> None:
    """화면은 문 앞에서 기다리지 않는다. 기다리면 누른 사람이 몇 분 동안 답을 못 받는다."""
    workflow_id = add_workflow(conn)
    gate = RunGate(lambda: 1)
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    sent: list[Coroutine[Any, Any, None]] = []

    async with gate.slot():
        response = await ui_workflows.run_now_fragment(
            request=request,
            workflow_id=workflow_id,
            conn=conn,
            scheduler=scheduler,
            fetcher=stub_fetcher(),
            gate=gate,
            launch=sent_to(sent),
            connect=lambda: conn,
        )

    html = bytes(response.body).decode()
    assert "동시 실행 상한(1)에 걸렸다" in html
    assert sent == []
    assert counts(conn, workflow_id) == (0, 0)


def test_실패한_실행은_사유까지_적는다(
    client: TestClient, conn: sqlite3.Connection, started: list[Coroutine[Any, Any, None]]
) -> None:
    workflow_id = add_workflow(conn, selectors=MISSING_SELECTORS)

    client.post(f"/ui/workflows/{workflow_id}/run")
    run_background(started)
    html = client.get(f"/ui/workflows/{workflow_id}/card?polled=true").text

    assert "실패" in html
    assert "selector_miss" in html
    assert "최근 실패 사유" in html
    run = conn.execute(
        "SELECT status, error_class FROM crawl_runs WHERE workflow_id = ?", (workflow_id,)
    ).fetchone()
    assert run["status"] == "failed"
    assert run["error_class"] == "selector_miss"


def test_없는_워크플로우는_실행하지_않는다(client: TestClient, conn: sqlite3.Connection) -> None:
    html = client.post("/ui/workflows/999/run").text

    assert "워크플로우 999 가 없다" in html
    assert conn.execute("SELECT COUNT(*) AS n FROM crawl_runs").fetchone()["n"] == 0

    card = client.get("/ui/workflows/999/card").text
    assert "워크플로우 999 가 없다" in card
