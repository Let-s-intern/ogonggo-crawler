"""사이트 전체 원문 다시 수집 (2026-10-02, `app/api/ui_recollect_all.py`).

실사이트에도 모델에도 나가지 않는다. 사이트 하나의 다시 수집은 부른 순서만 적는 가짜로 바꾼다.

| 확인 | 깨지면 |
|---|---|
| 공고를 담은 사이트만 한 곳씩 차례로 돈다 | 빈 사이트까지 돌거나 겹쳐 돈다 |
| 마감 무시 체크가 사이트마다 전해진다 | 잘못 읽은 마감일을 바로잡지 못한다 |
| 실패한 사이트를 적고 다음 사이트로 간다 | 한 곳 실패로 전체가 멈춘다 |
| 이미 돌고 있는 사이트는 건너뛴다 | 같은 사이트를 두 번 돈다 |
| 멈추면 남은 사이트를 돌지 않는다 | 멈출 길이 없다 |
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Coroutine, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api import ui_recollect_all, ui_workflows
from app.crawler import recollect
from app.crawler.runner import RunResult
from tests.test_ui_workflow_recollect import client, conn, scheduler, started  # noqa: F401
from tests.test_ui_workflow_run import add_workflow, run_background


@pytest.fixture(autouse=True)
def fresh_progress() -> Iterator[None]:
    ui_recollect_all._progress = ui_recollect_all.AllProgress()
    yield
    ui_recollect_all._progress = ui_recollect_all.AllProgress()


def add_job(connection: sqlite3.Connection, workflow_id: int, seq: int) -> None:
    connection.execute(
        "INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash)"
        " VALUES (?, ?, ?, ?)",
        (workflow_id, f"https://x/{workflow_id}/{seq}", json.dumps({"title": "t"}), f"h{seq}"),
    )


def fake_recollect(
    monkeypatch: pytest.MonkeyPatch, statuses: dict[int, str] | None = None
) -> list[tuple[int, bool]]:
    called: list[tuple[int, bool]] = []

    async def recollect_workflow(
        _conn: sqlite3.Connection, workflow_id: int, *, include_closed: bool = False, **_: Any
    ) -> RunResult:
        called.append((workflow_id, include_closed))
        status = (statuses or {}).get(workflow_id, "success")
        return RunResult(run_id=0, status=status, error_message="목록을 못 읽었다")

    monkeypatch.setattr(recollect, "recollect_workflow", recollect_workflow)
    return called


def test_공고를_담은_사이트만_한_곳씩_돌고_마감_무시를_전한다(
    client: TestClient,  # noqa: F811
    conn: sqlite3.Connection,  # noqa: F811
    started: list[Coroutine[Any, Any, None]],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, empty, second = (add_workflow(conn, name) for name in ("가", "빈 사이트", "나"))
    add_job(conn, first, 1)
    add_job(conn, second, 2)
    called = fake_recollect(monkeypatch, {second: "failed"})

    panel = client.get("/ui/sites/recollect-all").text
    assert "전체 사이트 원문 다시 수집 (2곳)" in panel

    started_html = client.post("/ui/sites/recollect-all", data={"include_closed": "1"}).text
    assert "0 / 2 사이트" in started_html
    run_background(started)

    assert called == [(first, True), (second, True)]
    assert empty not in [workflow_id for workflow_id, _ in called]
    done = client.get("/ui/sites/recollect-all").text
    assert "2 / 2 사이트 끝냄" in done
    assert "나 — 목록을 못 읽었다" in done


def test_이미_돌고_있는_사이트는_건너뛴다(
    client: TestClient,  # noqa: F811
    conn: sqlite3.Connection,  # noqa: F811
    started: list[Coroutine[Any, Any, None]],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    busy, free = add_workflow(conn, "도는 중"), add_workflow(conn, "쉬는 중")
    add_job(conn, busy, 1)
    add_job(conn, free, 2)
    called = fake_recollect(monkeypatch)
    ui_workflows._running.add(busy)

    client.post("/ui/sites/recollect-all")
    run_background(started)

    assert called == [(free, False)]
    assert "도는 중 — 이미 수집이 돌고 있었다" in client.get("/ui/sites/recollect-all").text


def test_멈추면_남은_사이트를_돌지_않는다(
    client: TestClient,  # noqa: F811
    conn: sqlite3.Connection,  # noqa: F811
    started: list[Coroutine[Any, Any, None]],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("가", "나"):
        add_job(conn, add_workflow(conn, name), 1 if name == "가" else 2)
    called = fake_recollect(monkeypatch)

    client.post("/ui/sites/recollect-all")
    client.post("/ui/sites/recollect-all/stop")
    run_background(started)

    assert called == []
    assert "(멈춤)" in client.get("/ui/sites/recollect-all").text
