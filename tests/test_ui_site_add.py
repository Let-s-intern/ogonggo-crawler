"""사이트 추가 — 걸면 요청 밖에서 등록 → 시험 수집 → 자동 수집 시작 (2026-09-17, LC-3344).

실사이트에 나가지 않는다. 등록과 시험 수집은 가짜로 바꿔 끼우고, 요청 밖 작업은 테스트가 받아
두었다가 직접 돌린다. 여기서 보는 것은 창이 바로 돌아오는지, 끝난 결과가 목록에 어떻게 남는지다.

| 확인 | 깨지면 |
|---|---|
| 주소와 회사 이름이 없으면 걸지 않는다 | AI 호출이 헛돈다 |
| 걸면 기다리지 않고 돌아오고 목록 맨 위에 추가 중으로 보인다 | 한 번에 한 곳밖에 못 넣는다 |
| 시험 수집이 되면 고른 주기로 자동 수집까지 시작한다 | 된 사이트를 한 번 더 눌러야 한다 |
| 안 되면 목록에 쉬운 말 사유와 다시 찾기가 남는다 | 창을 닫으면 실패를 모른다 |
| 다시 찾기는 초안을 지우고 공고 주소를 넣어 다시 건다 | 초안이 쌓인다 |
| 지우기는 줄과 초안을 같이 지운다 | 실패한 줄이 계속 남는다 |
"""

from __future__ import annotations

import asyncio
import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import db
from app.api import crawlers as crawlers_api
from app.api import site_adds, ui_site_add
from app.api import workflows as workflows_api
from app.main import app
from app.scheduler import WorkflowScheduler

LIST_URL = "https://careers.example.com/jobs"


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
def launched() -> list[Any]:
    return []


@pytest.fixture
def client(
    tmp_path: pathlib.Path,
    conn: sqlite3.Connection,
    scheduler: WorkflowScheduler,
    launched: list[Any],
) -> Iterator[TestClient]:
    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(tmp_path / "jobs.db")
        try:
            yield connection
        finally:
            connection.close()

    app.dependency_overrides[workflows_api.get_connection] = request_connection
    app.dependency_overrides[crawlers_api.get_connection] = request_connection
    app.dependency_overrides[workflows_api.get_workflow_scheduler] = lambda: scheduler
    app.dependency_overrides[crawlers_api.get_generator] = lambda: None
    app.dependency_overrides[crawlers_api.get_discoverer] = lambda: None
    app.dependency_overrides[crawlers_api.get_crawl_fetcher] = lambda: None
    app.dependency_overrides[ui_site_add.get_add_launcher] = lambda: launched.append
    app.dependency_overrides[ui_site_add.get_add_connect] = lambda: (
        lambda: db.connect(tmp_path / "jobs.db")
    )
    site_adds.clear()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()
        for coro in launched:
            coro.close()
        site_adds.clear()


class Fake:
    """등록은 크롤러 행만 만들고, 시험 수집은 정해 둔 결과를 돌려준다."""

    def __init__(self, conn: sqlite3.Connection, *, found: bool) -> None:
        self.conn = conn
        self.found = found
        self.registered: list[Any] = []

    async def create(self, payload: Any, conn: sqlite3.Connection, *_: Any) -> Any:
        self.registered.append(payload)
        cursor = conn.execute(
            "INSERT INTO crawlers (name, list_url, detail_url, status)"
            " VALUES ('예시', ?, ?, 'draft')",
            (payload.list_url, payload.detail_url or None),
        )
        crawler_id = int(cursor.lastrowid or 0)

        class Created:
            id = crawler_id
            list_mode = "static"

        return Created()

    async def run(self, crawler_id: int, conn: sqlite3.Connection, *_: Any) -> Any:
        if self.found:
            conn.execute("UPDATE crawlers SET status = 'tested' WHERE id = ?", (crawler_id,))
        return crawlers_api.TestRunOut(
            crawler_id=crawler_id,
            run_id=1,
            status="success" if self.found else "failed",
            crawler_status="tested" if self.found else "draft",
            render_mode="static",
            saved_render_mode="static",
            matched=14,
            success_count=3 if self.found else 0,
            new_count=0,
            fail_count=0 if self.found else 3,
            skipped_count=0,
            error_class=None if self.found else "detail_unreachable",
            error_message="" if self.found else "상세로 갈 길이 없다",
            items=[
                crawlers_api.PreviewItem(
                    source_url=f"{LIST_URL}/1",
                    state="preview",
                    fields={"title": "백엔드 개발자", "body": "본문 " * 150},
                )
            ]
            if self.found
            else [],
            failures=[],
        )


def install(monkeypatch: pytest.MonkeyPatch, fake: Fake) -> None:
    monkeypatch.setattr(crawlers_api, "create_crawler", fake.create)
    monkeypatch.setattr(crawlers_api, "test_run", fake.run)


def finish(launched: list[Any]) -> None:
    """받아 둔 요청 밖 작업을 차례로 끝까지 돌린다."""
    while launched:
        asyncio.run(launched.pop(0))


def test_사이트_목록의_추가_버튼이_창을_연다(client: TestClient) -> None:
    body = client.get("/workflows").text
    form = client.get("/ui/sites/new").text

    assert 'hx-get="/ui/sites/new"' in body
    assert 'name="list_url"' in form and 'name="company"' in form
    assert 'name="interval_minutes"' in form


def test_주소와_회사_이름이_없으면_걸지_않는다(
    client: TestClient,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    launched: list[Any],
) -> None:
    install(monkeypatch, Fake(conn, found=True))

    html = client.post("/ui/sites/new", data={"list_url": "careers", "company": ""}).text

    assert "모두 넣어 주세요" in html
    assert launched == []


def test_걸면_기다리지_않고_돌아오고_목록_맨_위에_추가_중으로_보인다(
    client: TestClient,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    launched: list[Any],
) -> None:
    fake = Fake(conn, found=True)
    install(monkeypatch, fake)

    response = client.post("/ui/sites/new", data={"list_url": LIST_URL, "company": "예시"})
    client.post("/ui/sites/new", data={"list_url": f"{LIST_URL}/2", "company": "둘째"})

    assert "추가를 시작했어요" in response.text
    assert response.headers["HX-Trigger"] == "site-added"
    assert fake.registered == []
    assert len(launched) == 2
    listing = client.get("/ui/sites").text
    assert listing.count('class="site-add-row') == 2
    assert "기다리는 중" in listing
    assert 'hx-trigger="every 3s"' in listing


def test_시험_수집이_되면_고른_주기로_자동_수집까지_시작한다(
    client: TestClient,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    launched: list[Any],
) -> None:
    install(monkeypatch, Fake(conn, found=True))
    client.post(
        "/ui/sites/new",
        data={"list_url": LIST_URL, "company": "예시", "interval_minutes": "180"},
    )

    finish(launched)

    row = conn.execute("SELECT name, interval_minutes, status FROM workflows").fetchone()
    assert (row["name"], row["interval_minutes"], row["status"]) == ("예시", 180, "active")
    listing = client.get("/ui/sites").text
    assert "site-add-row" not in listing
    assert 'hx-trigger="every 3s"' not in listing


def test_안_되면_목록에_쉬운_말_사유와_다시_찾기가_남는다(
    client: TestClient,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    launched: list[Any],
) -> None:
    install(monkeypatch, Fake(conn, found=False))
    client.post("/ui/sites/new", data={"list_url": LIST_URL, "company": "예시"})

    finish(launched)

    listing = client.get("/ui/sites").text
    assert "상세 페이지로 들어가지 못했어요" in listing
    assert 'hx-get="/ui/sites/new/1"' in listing
    assert 'hx-trigger="every 3s"' not in listing
    retry = client.get("/ui/sites/new/1").text
    assert "공고 하나의 주소로 다시 찾기" in retry
    assert 'name="replace_add_id" value="1"' in retry
    assert conn.execute("SELECT count(*) FROM workflows").fetchone()[0] == 0


def test_등록이_거절되면_사유를_남긴다(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, launched: list[Any]
) -> None:
    async def refuse(*_: Any) -> Any:
        raise HTTPException(
            status_code=422, detail={"reason": "list_not_found", "message": "목록이 없다"}
        )

    monkeypatch.setattr(crawlers_api, "create_crawler", refuse)
    client.post("/ui/sites/new", data={"list_url": LIST_URL, "company": "예시"})

    finish(launched)

    assert "공고 목록을 찾지 못했어요" in client.get("/ui/sites").text


def test_다시_찾기는_초안을_지우고_공고_주소를_넣어_다시_건다(
    client: TestClient,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    launched: list[Any],
) -> None:
    fake = Fake(conn, found=False)
    install(monkeypatch, fake)
    client.post("/ui/sites/new", data={"list_url": LIST_URL, "company": "예시"})
    finish(launched)

    fake.found = True
    client.post(
        "/ui/sites/new",
        data={
            "list_url": LIST_URL,
            "company": "예시",
            "detail_url": f"{LIST_URL}/10",
            "replace_add_id": "1",
        },
    )
    finish(launched)

    assert fake.registered[-1].detail_url == f"{LIST_URL}/10"
    ids = [int(row["id"]) for row in conn.execute("SELECT id FROM crawlers ORDER BY id")]
    assert ids == [2]
    assert conn.execute("SELECT count(*) FROM workflows").fetchone()[0] == 1
    assert "site-add-row" not in client.get("/ui/sites").text


def test_지우기는_줄과_초안을_같이_지운다(
    client: TestClient,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    launched: list[Any],
) -> None:
    install(monkeypatch, Fake(conn, found=False))
    client.post("/ui/sites/new", data={"list_url": LIST_URL, "company": "예시"})
    finish(launched)

    response = client.post("/ui/sites/new/1/dismiss")

    assert response.headers["HX-Trigger"] == "site-added"
    assert "site-add-row" not in client.get("/ui/sites").text
    assert conn.execute("SELECT count(*) FROM crawlers").fetchone()[0] == 0
