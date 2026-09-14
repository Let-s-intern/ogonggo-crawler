"""카드에 적히는 주기와 실행 출처 (18.2).

2026-08-22 에 5개 워크플로우를 30분 주기로 돌려 놓고 화면을 보니, 입력칸에 숫자는 있어도
"30분마다 돈다" 는 문장도 다음 실행 시각도 없었다. 최근 실행이 사람이 누른 것인지 스케줄러가
깨운 것인지도 구분되지 않아, 주기가 실제로 도는지 확인할 방법이 없었다.

| 확인 | 깨지면 |
|---|---|
| 주기를 문장으로 적는다 | 입력칸의 숫자를 보고 사람이 뜻을 짐작한다 |
| 다음 실행 예정 시각을 적는다 | 지금 도는 중인지 기다리는 중인지 알 수 없다 |
| 중지된 워크플로우는 다음 실행이 없다고 적는다 | 빈 칸이 "모른다" 로 읽혀 조치가 갈린다 |
| 최근 실행 옆에 출처를 단어로 적는다 | 주기가 죽어 있어도 최근 실행이 있으면 도는 것처럼 보인다 |
| 기록되지 않은 출처는 `알 수 없음` 이다 | 추측한 값이 사실인 것처럼 화면에 남는다 |
| 예정 시각을 운영자 시간대로 적는다 | UTC 가 그대로 떠 9시간 전을 가리킨다 (21.2) |

예정 시각은 스케줄러의 잡에서 읽는다. 기동 전 잡에는 그 값이 없으므로 테스트가 직접 넣는다 —
마지막 실행에 주기를 더해 추측하는 계산을 여기에 만들지 않는다 (`app/scheduler.py`).
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sqlite3
from collections.abc import Coroutine, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi.testclient import TestClient

from app import db
from app.api import crawlers as crawlers_api
from app.api import ui_workflows
from app.api import workflows as workflows_api
from app.api.ui import display_zone
from app.config import Settings
from app.crawler.fetcher import Fetcher
from app.main import app
from app.scheduler import RunGate, WorkflowScheduler, job_id

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
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "jobs.db"


@pytest.fixture
def conn(db_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(db_path)
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
def started() -> Iterator[list[Coroutine[Any, Any, None]]]:
    sent: list[Coroutine[Any, Any, None]] = []
    try:
        yield sent
    finally:
        for coro in sent:
            coro.close()


@pytest.fixture
def client(
    db_path: pathlib.Path,
    conn: sqlite3.Connection,
    scheduler: WorkflowScheduler,
    started: list[Coroutine[Any, Any, None]],
) -> Iterator[TestClient]:
    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(db_path)
        try:
            yield connection
        finally:
            connection.close()

    def launch(coro: Coroutine[Any, Any, None]) -> None:
        started.append(coro)

    fetcher = stub_fetcher()
    app.dependency_overrides[workflows_api.get_connection] = request_connection
    app.dependency_overrides[crawlers_api.get_connection] = request_connection
    app.dependency_overrides[crawlers_api.get_crawl_fetcher] = lambda: fetcher
    app.dependency_overrides[workflows_api.get_workflow_scheduler] = lambda: scheduler
    app.dependency_overrides[ui_workflows.get_run_gate] = lambda: RunGate(lambda: 2)
    app.dependency_overrides[ui_workflows.get_run_launcher] = lambda: launch
    app.dependency_overrides[ui_workflows.get_run_connect] = lambda: lambda: db.connect(db_path)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        ui_workflows._running.clear()


def add_workflow(conn: sqlite3.Connection, *, minutes: int = 30) -> int:
    cursor = conn.execute(
        """
        INSERT INTO crawlers (name, list_url, detail_url, selectors_json, status)
        VALUES (?, ?, ?, ?, 'promoted')
        """,
        (
            "python.org",
            LIST_URL,
            "https://www.python.org/jobs/8126/",
            json.dumps(SELECTORS),
        ),
    )
    crawler_id = int(cursor.lastrowid or 0)
    cursor = conn.execute(
        """
        INSERT INTO workflows (crawler_id, name, interval_minutes, status)
        VALUES (?, ?, ?, 'active')
        """,
        (crawler_id, "python.org 채용", minutes),
    )
    return int(cursor.lastrowid or 0)


def add_run(conn: sqlite3.Connection, workflow_id: int, trigger: str | None) -> None:
    conn.execute(
        """
        INSERT INTO crawl_runs (workflow_id, finished_at, status, trigger)
        VALUES (?, datetime('now'), 'success', ?)
        """,
        (workflow_id, trigger),
    )
    conn.execute("UPDATE workflows SET last_run_at = datetime('now') WHERE id = ?", (workflow_id,))


def schedule_next(scheduler: WorkflowScheduler, workflow_id: int, when: datetime) -> None:
    """잡의 다음 실행 예정 시각을 정한다. 기동 전 잡에는 그 값이 아직 없다."""
    job = scheduler.scheduler.get_job(job_id(workflow_id))
    assert job is not None
    job.next_run_time = when


def run_background(sent: list[Coroutine[Any, Any, None]]) -> None:
    while sent:
        asyncio.run(sent.pop(0))


def test_주기를_문장으로_적는다(
    client: TestClient, conn: sqlite3.Connection, scheduler: WorkflowScheduler
) -> None:
    add_workflow(conn, minutes=30)
    scheduler.sync(conn)

    html = client.get("/ui/workflows").text

    assert "30분마다 돈다" in html


def test_다음_실행_예정_시각이_카드에_있다(
    client: TestClient, conn: sqlite3.Connection, scheduler: WorkflowScheduler
) -> None:
    workflow_id = add_workflow(conn, minutes=30)
    scheduler.sync(conn)
    when = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=12)
    schedule_next(scheduler, workflow_id, when)

    html = client.get("/ui/workflows").text

    # 저장은 UTC, 화면은 운영자 시간대다. 약칭까지 적어야 9시간 차이를 다시 의심하지 않는다
    local = when.astimezone(display_zone())
    assert f"다음 실행 예정 {local.strftime('%Y-%m-%d %H:%M:%S %Z')}" in html
    # 시각만 적으면 화면을 보는 사람이 매번 뺄셈을 한다
    assert "약 11분 뒤" in html or "약 12분 뒤" in html


def test_예정_시각을_모르면_모른다고_적는다(
    client: TestClient, conn: sqlite3.Connection, scheduler: WorkflowScheduler
) -> None:
    """마지막 실행에 주기를 더해 추측하지 않는다. 틀린 시각은 빈 칸보다 나쁘다."""
    add_workflow(conn, minutes=30)
    scheduler.sync(conn)

    html = client.get("/ui/workflows").text

    assert "다음 실행 예정 시각을 스케줄러에서 읽지 못했다" in html


def test_중지하면_다음_실행이_없다고_적는다(
    client: TestClient, conn: sqlite3.Connection, scheduler: WorkflowScheduler
) -> None:
    """빈 칸으로 두면 언제 도는지 모르는 것과 구분되지 않는다."""
    workflow_id = add_workflow(conn, minutes=30)
    scheduler.sync(conn)
    schedule_next(scheduler, workflow_id, datetime.now(UTC) + timedelta(minutes=12))

    html = client.patch(f"/ui/workflows/{workflow_id}", data={"status": "paused"}).text

    assert "중지됨. 다음 실행 없음 — 재개하면 30분마다 돈다" in html
    assert "다음 실행 예정" not in html


@pytest.mark.parametrize(
    ("trigger", "word"),
    [("schedule", "주기 실행"), ("manual", "수동 1회"), (None, "알 수 없음")],
)
def test_최근_실행_옆에_출처가_단어로_붙는다(
    client: TestClient, conn: sqlite3.Connection, trigger: str | None, word: str
) -> None:
    workflow_id = add_workflow(conn)
    add_run(conn, workflow_id, trigger)

    html = client.get("/ui/workflows").text

    assert word in html


def test_실행_기록이_없으면_출처_자리도_없다(client: TestClient, conn: sqlite3.Connection) -> None:
    add_workflow(conn)

    html = client.get("/ui/workflows").text

    assert "실행 기록 없음" in html
    assert "알 수 없음" not in html


def test_1회_실행을_누르면_출처가_수동_1회로_뜬다(
    client: TestClient,
    conn: sqlite3.Connection,
    scheduler: WorkflowScheduler,
    started: list[Coroutine[Any, Any, None]],
) -> None:
    """스케줄러가 깨운 실행과 사람이 누른 실행이 카드에서 갈리는지가 이 화면의 핵심이다."""
    workflow_id = add_workflow(conn)
    scheduler.sync(conn)

    client.post(f"/ui/workflows/{workflow_id}/run")
    run_background(started)
    html = client.get(f"/ui/workflows/{workflow_id}/card").text

    assert "수동 1회" in html
    assert "주기 실행" not in html
