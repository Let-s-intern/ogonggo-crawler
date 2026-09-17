"""사이트 추가 창 — 등록 → 시험 수집 → 자동 수집 시작 (2026-09-17, LC-3344).

실사이트에 나가지 않는다. 등록과 시험 수집은 가짜로 바꿔 끼운다. 여기서 보는 것은 창이 결과에
따라 어느 단계로 가는지와, 다시 찾기가 초안을 정리하는지다.

| 확인 | 깨지면 |
|---|---|
| 주소와 회사 이름이 없으면 등록하지 않는다 | AI 호출이 헛돈다 |
| 시험 수집이 되면 공고 미리보기와 주기, 시작 버튼이 나온다 | 된 사이트를 시작할 길이 없다 |
| 안 되면 쉬운 말 사유와 공고 주소로 다시 찾기가 나온다 | 막힌 채로 창이 끝난다 |
| 다시 찾기는 방금 만든 초안을 지우고 공고 주소를 넣어 다시 등록한다 | 초안이 쌓인다 |
| 시작하면 워크플로우가 생기고 목록을 다시 부른다 | 시작했는데 목록에 없다 |
"""

from __future__ import annotations

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
def client(
    tmp_path: pathlib.Path, conn: sqlite3.Connection, scheduler: WorkflowScheduler
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
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


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


def test_사이트_목록의_추가_버튼이_창을_연다(client: TestClient) -> None:
    body = client.get("/workflows").text
    form = client.get("/ui/sites/new").text

    assert 'hx-get="/ui/sites/new"' in body
    assert 'name="list_url"' in form and 'name="company"' in form
    assert "찾아서 시험 수집" in form


def test_주소와_회사_이름이_없으면_등록하지_않는다(
    client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = Fake(conn, found=True)
    install(monkeypatch, fake)

    html = client.post("/ui/sites/new", data={"list_url": "careers", "company": ""}).text

    assert "모두 넣어 주세요" in html
    assert fake.registered == []


def test_시험_수집이_되면_미리보기와_시작_버튼이_나온다(
    client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, Fake(conn, found=True))

    html = client.post("/ui/sites/new", data={"list_url": LIST_URL, "company": "예시"}).text

    assert "찾았어요" in html
    assert "백엔드 개발자" in html
    assert 'name="interval_minutes"' in html
    assert "자동 수집 시작" in html


def test_안_되면_쉬운_말_사유와_공고_주소로_다시_찾기가_나온다(
    client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, Fake(conn, found=False))

    html = client.post("/ui/sites/new", data={"list_url": LIST_URL, "company": "예시"}).text

    assert "상세 페이지로 들어가지 못했어요" in html
    assert "공고 하나의 주소로 다시 찾기" in html
    assert 'name="replace_crawler_id" value="1"' in html


def test_다시_찾기는_초안을_지우고_공고_주소를_넣어_다시_등록한다(
    client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = Fake(conn, found=False)
    install(monkeypatch, fake)
    client.post("/ui/sites/new", data={"list_url": LIST_URL, "company": "예시"})

    fake.found = True
    html = client.post(
        "/ui/sites/new",
        data={
            "list_url": LIST_URL,
            "company": "예시",
            "detail_url": f"{LIST_URL}/10",
            "replace_crawler_id": "1",
        },
    ).text

    assert "찾았어요" in html
    assert fake.registered[-1].detail_url == f"{LIST_URL}/10"
    ids = [int(row["id"]) for row in conn.execute("SELECT id FROM crawlers ORDER BY id")]
    assert ids == [2]


def test_등록이_거절되면_사유를_적는다(
    client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(*_: Any) -> Any:
        raise HTTPException(
            status_code=422, detail={"reason": "list_not_found", "message": "목록이 없다"}
        )

    monkeypatch.setattr(crawlers_api, "create_crawler", refuse)

    html = client.post("/ui/sites/new", data={"list_url": LIST_URL, "company": "예시"}).text

    assert "공고 목록을 찾지 못했어요" in html
    assert "공고 하나의 주소로 다시 찾기" in html


def test_시작하면_워크플로우가_생기고_목록을_다시_부른다(
    client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    install(monkeypatch, Fake(conn, found=True))
    client.post("/ui/sites/new", data={"list_url": LIST_URL, "company": "예시"})

    response = client.post(
        "/ui/sites/new/1/start", data={"name": "예시 채용", "interval_minutes": "60"}
    )

    assert "자동 수집을 시작했어요" in response.text
    assert response.headers["HX-Trigger"] == "site-added"
    row = conn.execute("SELECT name, interval_minutes FROM workflows").fetchone()
    assert (row["name"], row["interval_minutes"]) == ("예시 채용", 60)
