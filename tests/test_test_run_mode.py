"""테스트 실행 화면의 모드 전환과 한 번만 시험하기 (13.2).

실사이트에도 브라우저에도 나가지 않는다. 정적 경로에는 목록이 비어 있는 껍데기를, 렌더
경로에는 항목이 채워진 python.org 픽스처를 돌려준다 — JS 로 목록을 그리는 사이트의 모양
그대로다. 그래서 같은 크롤러를 두 모드로 돌리면 필드별 매칭 수가 갈린다.

확인하는 것은 셋이다.

| 확인 | 왜 |
|---|---|
| 시험이 저장값을 안 바꾼다 | 비교가 이 화면의 일이다. 시험이 저장을 겸하면 비교가 안 된다 |
| 두 모드의 매칭 수가 갈린다 | 어느 쪽이 필요한지 판단할 근거가 이 숫자다 |
| 저장 모드 전환은 따로 있다 | 정하고 나면 워크플로우가 읽는 값을 옮겨야 한다 |
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import crawlers as crawlers_api
from app.config import Settings
from app.crawler import playwright as render_module
from app.crawler.fetcher import Fetcher, FetchPolicy, FetchResult
from app.main import app

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
LIST_HTML = (FIXTURES / "pythonorg-jobs-list-20260821.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "pythonorg-job-detail-20260821.html").read_text(encoding="utf-8")
# 목록 컨테이너만 있고 항목은 스크립트가 채우는 사이트. 정적 fetch 에는 공고가 없다
SHELL_HTML = (FIXTURES / "js-rendered-list-shell-20260822.html").read_text(encoding="utf-8")

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
    """정적 경로. 목록 자리에 껍데기를 돌려준다."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        if request.url.path == "/jobs/":
            return httpx.Response(200, text=SHELL_HTML)
        return httpx.Response(200, text=DETAIL_HTML)

    async def no_wait(seconds: float) -> None:
        return None

    return Fetcher(
        settings=Settings(crawl_delay_seconds=0.0, crawl_max_retries=0),
        transport=httpx.MockTransport(handle),
        sleep=no_wait,
    )


class SpyRenderer:
    """`Renderer` 자리에 들어가는 대역. 브라우저 없이 채워진 목록을 돌려준다."""

    built: list[SpyRenderer] = []

    def __init__(self, fetcher: FetchPolicy) -> None:
        self.rendered: list[str] = []
        self.closed = False
        SpyRenderer.built.append(self)

    async def fetch(self, url: str) -> FetchResult:
        self.rendered.append(url)
        html = LIST_HTML if url == LIST_URL else DETAIL_HTML
        return FetchResult(url=url, status_code=200, text=html)

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def spy(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[SpyRenderer]]:
    """진짜 브라우저가 뜨지 않게 막는다. 뜨려고만 해도 기록에 남는다."""
    SpyRenderer.built = []
    monkeypatch.setattr(render_module, "Renderer", SpyRenderer)
    yield SpyRenderer
    SpyRenderer.built = []


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def client(tmp_path: pathlib.Path, conn: sqlite3.Connection) -> Iterator[TestClient]:
    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(tmp_path / "jobs.db")
        try:
            yield connection
        finally:
            connection.close()

    fetcher = stub_fetcher()
    app.dependency_overrides[crawlers_api.get_connection] = request_connection
    app.dependency_overrides[crawlers_api.get_crawl_fetcher] = lambda: fetcher
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def add_crawler(conn: sqlite3.Connection, render_mode: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO crawlers
               (name, list_url, detail_url, selectors_json, status, list_mode, detail_mode)
        VALUES ('python.org', ?, ?, ?, 'draft', ?, ?)
        """,
        (
            LIST_URL,
            "https://www.python.org/jobs/8126/",
            json.dumps(SELECTORS),
            render_mode,
            render_mode,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def saved_mode(conn: sqlite3.Connection, crawler_id: int) -> str:
    row = conn.execute("SELECT list_mode FROM crawlers WHERE id = ?", (crawler_id,)).fetchone()
    return str(row["list_mode"])


def crawler_status(conn: sqlite3.Connection, crawler_id: int) -> str:
    row = conn.execute("SELECT status FROM crawlers WHERE id = ?", (crawler_id,)).fetchone()
    return str(row["status"])


def test_모드를_안_주면_저장된_모드로_돈다(client: TestClient, conn: sqlite3.Connection) -> None:
    crawler_id = add_crawler(conn, "static")

    body = client.post(f"/api/crawlers/{crawler_id}/test-run?limit=1").json()

    assert (body["render_mode"], body["saved_render_mode"]) == ("static", "static")
    assert SpyRenderer.built == []


def test_한_번만_시험해도_저장값은_그대로다(client: TestClient, conn: sqlite3.Connection) -> None:
    """비교가 이 화면의 일이다. 시험할 때마다 저장값이 따라 바뀌면 비교가 안 된다."""
    crawler_id = add_crawler(conn, "static")

    body = client.post(f"/api/crawlers/{crawler_id}/test-run?limit=1&render_mode=playwright").json()

    assert body["render_mode"] == "playwright"
    assert body["saved_render_mode"] == "static"
    assert len(SpyRenderer.built) == 1
    assert saved_mode(conn, crawler_id) == "static"


def test_두_모드의_매칭_수가_갈린다(client: TestClient, conn: sqlite3.Connection) -> None:
    """정적은 껍데기라 0건, 렌더는 목록이 채워져 있다. 이 숫자가 판단 근거다."""
    crawler_id = add_crawler(conn, "static")

    static_run = client.post(f"/api/crawlers/{crawler_id}/test-run?limit=1").json()
    render_run = client.post(
        f"/api/crawlers/{crawler_id}/test-run?limit=1&render_mode=playwright"
    ).json()

    assert static_run["matched"] == 0
    assert static_run["status"] == "failed"
    assert static_run["error_class"] == "selector_miss"
    assert render_run["matched"] == 25
    assert render_run["status"] == "success"


def test_한_번만_시험한_실행은_상태를_올리지_않는다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """저장된 모드로 돌 때 어떻게 되는지를 말해 주지 않는 실행이다."""
    crawler_id = add_crawler(conn, "static")

    body = client.post(f"/api/crawlers/{crawler_id}/test-run?limit=1&render_mode=playwright").json()

    assert body["status"] == "success"
    assert body["crawler_status"] == "draft"
    assert crawler_status(conn, crawler_id) == "draft"


def test_저장된_모드로_통과하면_상태가_올라간다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    crawler_id = add_crawler(conn, "playwright")

    body = client.post(f"/api/crawlers/{crawler_id}/test-run?limit=1").json()

    assert body["status"] == "success"
    assert crawler_status(conn, crawler_id) == "tested"


def test_모르는_모드는_거절한다(client: TestClient, conn: sqlite3.Connection) -> None:
    """조용히 저장된 모드로 돌리면 운영자는 다른 것을 시험한 줄 안다."""
    crawler_id = add_crawler(conn, "static")

    response = client.post(f"/api/crawlers/{crawler_id}/test-run?render_mode=selenium")

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "unknown_render_mode"


def test_화면에서_저장_모드를_바꾼다(client: TestClient, conn: sqlite3.Connection) -> None:
    """등록 화면으로 돌아가지 않아도 되게 실행 대상 표에서 부른다."""
    crawler_id = add_crawler(conn, "static")

    response = client.put(
        f"/ui/test-targets/{crawler_id}/render-mode", data={"render_mode": "playwright"}
    )

    assert response.status_code == 200
    assert saved_mode(conn, crawler_id) == "playwright"
    # 갈아 끼운 표에 바뀐 값과 무엇을 했는지가 같이 들어온다
    assert "저장 모드를" in response.text
    assert "이번만 정적으로" in response.text


def test_화면_실행_폼이_모드를_그대로_넘긴다(client: TestClient, conn: sqlite3.Connection) -> None:
    crawler_id = add_crawler(conn, "static")

    response = client.post(
        f"/ui/crawlers/{crawler_id}/test-run", data={"limit": "1", "render_mode": "playwright"}
    )

    assert response.status_code == 200
    assert "이번 실행만" in response.text
    assert saved_mode(conn, crawler_id) == "static"
