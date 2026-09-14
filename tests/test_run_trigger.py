"""실행 출처 기록 (18.1).

`crawl_runs.trigger` 가 없으면 최근 실행이 있어도 그것이 주기가 돈 것인지 사람이 눌러 온
것인지 알 수 없다. 그래서 확인하는 것은 "컬럼이 있다" 가 아니라 **세 경로가 실제로 서로 다른
값을 남기는가** 다.

실사이트에 나가지 않는다. 저장된 python.org 픽스처를 돌려주는 스텁 fetch 클라이언트를 쓴다
(`.claude/rules/core.md`).

| 경로 | 부르는 곳 | 남는 값 |
|---|---|---|
| 주기 실행 | `WorkflowScheduler._execute` | `schedule` |
| 화면의 1회 실행 | `POST /ui/workflows/{id}/run` | `manual` |
| 테스트 실행 | `POST /api/crawlers/{id}/test-run` | `test` |
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sqlite3
from collections.abc import Coroutine, Iterator
from typing import Any

import httpx
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi.testclient import TestClient

from app import db
from app.api import crawlers as crawlers_api
from app.api import ui_workflows
from app.api import workflows as workflows_api
from app.config import Settings
from app.crawler import runner
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
def started() -> Iterator[list[Coroutine[Any, Any, None]]]:
    sent: list[Coroutine[Any, Any, None]] = []
    try:
        yield sent
    finally:
        for coro in sent:
            coro.close()


@pytest.fixture
def client(
    db_path: pathlib.Path, conn: sqlite3.Connection, started: list[Coroutine[Any, Any, None]]
) -> Iterator[TestClient]:
    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(db_path)
        try:
            yield connection
        finally:
            connection.close()

    def launch(coro: Coroutine[Any, Any, None]) -> None:
        started.append(coro)

    scheduler = WorkflowScheduler(scheduler=AsyncIOScheduler(timezone="UTC"), runner=_do_nothing)
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
        scheduler.shutdown()


async def _do_nothing(workflow_id: int) -> None:
    return None


def add_crawler(conn: sqlite3.Connection, status: str = "promoted") -> int:
    cursor = conn.execute(
        """
        INSERT INTO crawlers (name, list_url, detail_url, selectors_json, status)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "python.org",
            LIST_URL,
            "https://www.python.org/jobs/8126/",
            json.dumps(SELECTORS),
            status,
        ),
    )
    return int(cursor.lastrowid or 0)


def add_workflow(conn: sqlite3.Connection) -> int:
    crawler_id = add_crawler(conn)
    cursor = conn.execute(
        """
        INSERT INTO workflows (crawler_id, name, interval_minutes, status)
        VALUES (?, ?, 30, 'active')
        """,
        (crawler_id, "python.org 채용"),
    )
    return int(cursor.lastrowid or 0)


def triggers(conn: sqlite3.Connection) -> list[str | None]:
    rows = conn.execute("SELECT trigger FROM crawl_runs ORDER BY id").fetchall()
    return [row["trigger"] for row in rows]


def _use_tmp_db(monkeypatch: pytest.MonkeyPatch, db_path: pathlib.Path) -> None:
    """`db.connect()` 가 임시 DB 를 열게 한다. 잡 실행 경로는 연결을 스스로 연다."""
    real_connect = db.connect
    monkeypatch.setattr(db, "connect", lambda path=None: real_connect(db_path))


def run_background(sent: list[Coroutine[Any, Any, None]]) -> None:
    while sent:
        asyncio.run(sent.pop(0))


def test_0007_이전_행은_NULL_로_남는다(conn: sqlite3.Connection) -> None:
    """마이그레이션은 컬럼만 더한다. 기록되지 않은 출처를 지금 와서 추측해 채우지 않는다."""
    workflow_id = add_workflow(conn)
    conn.execute("INSERT INTO crawl_runs (workflow_id) VALUES (?)", (workflow_id,))

    assert triggers(conn) == [None]


def test_셋_밖의_값은_거절된다(conn: sqlite3.Connection) -> None:
    workflow_id = add_workflow(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO crawl_runs (workflow_id, trigger) VALUES (?, 'cron')", (workflow_id,)
        )


def test_역적용하면_컬럼이_사라진다(db_path: pathlib.Path) -> None:
    """0007 은 컬럼 하나만 더한다. 되돌리면 그 컬럼만 없어지고 실행 기록은 남는다."""
    connection = db.connect(db_path)
    try:
        db.migrate_up(connection)
        crawler_id = add_crawler(connection)
        connection.execute(
            "INSERT INTO crawl_runs (crawler_id, trigger) VALUES (?, 'test')", (crawler_id,)
        )

        # 0007 까지 내려가려면 그 뒤에 붙은 것을 먼저 되돌려야 한다. 마이그레이션이 하나
        # 붙을 때마다 이 숫자를 손으로 고치지 않도록 파일 목록에서 센다
        versions = [migration.version for migration in db.load_migrations()]
        db.migrate_down(connection, steps=len(versions) - versions.index("0007"))

        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(crawl_runs)").fetchall()
        }
        assert "trigger" not in columns
        assert connection.execute("SELECT COUNT(*) AS n FROM crawl_runs").fetchone()["n"] == 1
    finally:
        connection.close()


async def test_스케줄러가_부른_실행은_schedule_로_남는다(
    conn: sqlite3.Connection, db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """잡이 깨운 실행이다. 실제 잡 실행 경로(`_execute`)를 그대로 돌린다."""
    workflow_id = add_workflow(conn)
    _use_tmp_db(monkeypatch, db_path)
    monkeypatch.setattr(runner, "get_fetcher", stub_fetcher)

    scheduler = WorkflowScheduler(scheduler=AsyncIOScheduler(timezone="UTC"))
    await scheduler._execute(workflow_id)

    assert triggers(conn) == [runner.SCHEDULE]


def test_화면의_1회_실행은_manual_로_남는다(
    client: TestClient,
    conn: sqlite3.Connection,
    started: list[Coroutine[Any, Any, None]],
) -> None:
    workflow_id = add_workflow(conn)

    response = client.post(f"/ui/workflows/{workflow_id}/run")
    run_background(started)

    assert response.status_code == 200
    assert triggers(conn) == [runner.MANUAL]


def test_테스트_실행은_test_로_남는다(client: TestClient, conn: sqlite3.Connection) -> None:
    crawler_id = add_crawler(conn, status="draft")

    response = client.post(f"/api/crawlers/{crawler_id}/test-run?limit=1")

    assert response.status_code == 200
    assert triggers(conn) == [runner.TEST]


def test_세_경로가_같은_DB_에서_서로_다른_값을_남긴다(
    client: TestClient,
    conn: sqlite3.Connection,
    db_path: pathlib.Path,
    started: list[Coroutine[Any, Any, None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """한 번에 놓고 봤을 때 셋이 갈리는지가 이 컬럼을 더한 이유다."""
    workflow_id = add_workflow(conn)
    draft_id = add_crawler(conn, status="draft")

    client.post(f"/ui/workflows/{workflow_id}/run")
    run_background(started)
    client.post(f"/api/crawlers/{draft_id}/test-run?limit=1")

    _use_tmp_db(monkeypatch, db_path)
    monkeypatch.setattr(runner, "get_fetcher", stub_fetcher)
    scheduler = WorkflowScheduler(scheduler=AsyncIOScheduler(timezone="UTC"))
    asyncio.run(scheduler._execute(workflow_id))

    assert triggers(conn) == [runner.MANUAL, runner.TEST, runner.SCHEDULE]
