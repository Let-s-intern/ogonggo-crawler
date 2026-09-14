"""`crawlers.list_mode` 가 실행 경로를 고르는지 본다.

실사이트에도 브라우저에도 나가지 않는다. 정적 경로에는 저장된 python.org 픽스처를 돌려주는
스텁 fetch 클라이언트가, 렌더 경로에는 같은 픽스처를 돌려주는 대역이 들어간다. 둘이 각자
기록을 남기므로 실행이 어느 쪽으로 갔는지가 갈린다.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from app import db
from app.config import Settings
from app.crawler import playwright as render_module
from app.crawler.fetcher import Fetcher, FetchPolicy, FetchResult, TransportError
from app.crawler.runner import run_workflow

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


class CountingFetcher(Fetcher):
    """정적으로 몇 번 가져갔는지 센다. 나머지 동작은 공용 클라이언트 그대로다."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.fetched: list[str] = []

    async def fetch(self, url: str) -> FetchResult:
        self.fetched.append(url)
        return await super().fetch(url)


def stub_fetcher() -> CountingFetcher:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        if request.url.path == "/jobs/":
            return httpx.Response(200, text=LIST_HTML)
        return httpx.Response(200, text=DETAIL_HTML)

    async def no_wait(seconds: float) -> None:
        return None

    return CountingFetcher(
        settings=Settings(crawl_delay_seconds=0.0, crawl_max_retries=0),
        transport=httpx.MockTransport(handle),
        sleep=no_wait,
    )


class SpyRenderer:
    """`Renderer` 자리에 들어가는 대역. 브라우저 없이 픽스처를 돌려준다.

    정적 클라이언트에 넘기지 않는다. 그래야 러너가 어느 쪽으로 갔는지가 두 기록으로 갈린다.
    """

    built: list[SpyRenderer] = []
    error: Exception | None = None

    def __init__(self, fetcher: FetchPolicy) -> None:
        self._fetcher = fetcher
        self.rendered: list[str] = []
        self.closed = False
        SpyRenderer.built.append(self)

    async def fetch(self, url: str) -> FetchResult:
        self.rendered.append(url)
        if SpyRenderer.error is not None:
            raise SpyRenderer.error
        html = LIST_HTML if url == LIST_URL else DETAIL_HTML
        return FetchResult(url=url, status_code=200, text=html)

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def spy(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[SpyRenderer]]:
    """진짜 브라우저가 뜨지 않게 막는다. 뜨려고만 해도 기록에 남는다."""
    SpyRenderer.built = []
    SpyRenderer.error = None
    monkeypatch.setattr(render_module, "Renderer", SpyRenderer)
    yield SpyRenderer
    SpyRenderer.built = []
    SpyRenderer.error = None


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    try:
        yield connection
    finally:
        connection.close()


def add_workflow(conn: sqlite3.Connection, render_mode: str) -> int:
    conn.execute(
        """
        INSERT INTO crawlers (name, list_url, selectors_json, status, list_mode, detail_mode)
        VALUES (?, ?, ?, 'promoted', ?, ?)
        """,
        ("python.org", LIST_URL, json.dumps(SELECTORS), render_mode, render_mode),
    )
    cursor = conn.execute(
        "INSERT INTO workflows (crawler_id, name, interval_minutes) VALUES (1, ?, 60)",
        ("python.org 채용",),
    )
    return int(cursor.lastrowid or 0)


def test_the_default_render_mode_is_static(conn: sqlite3.Connection) -> None:
    """컬럼 기본값이 정적이다. 아무것도 적지 않은 크롤러가 브라우저를 띄우면 안 된다."""
    conn.execute("INSERT INTO crawlers (name, list_url) VALUES ('x', ?)", (LIST_URL,))
    row = conn.execute("SELECT list_mode, detail_mode FROM crawlers WHERE id = 1").fetchone()

    assert (row["list_mode"], row["detail_mode"]) == ("static", "static")


async def test_static_crawler_never_touches_the_render_path(conn: sqlite3.Connection) -> None:
    workflow_id = add_workflow(conn, "static")
    fetcher = stub_fetcher()

    result = await run_workflow(conn, workflow_id, fetcher=fetcher, limit=2)

    assert result.status == "success"
    assert SpyRenderer.built == []
    assert LIST_URL in fetcher.fetched


async def test_playwright_crawler_goes_through_the_render_path(conn: sqlite3.Connection) -> None:
    workflow_id = add_workflow(conn, "playwright")
    fetcher = stub_fetcher()

    result = await run_workflow(conn, workflow_id, fetcher=fetcher, limit=2)

    assert result.status == "success"
    assert len(SpyRenderer.built) == 1
    rendered = SpyRenderer.built[0]
    # 목록과 상세 2건이 전부 렌더 경로로 갔다. 러너가 직접 가져간 것은 없다
    assert rendered.rendered[0] == LIST_URL
    assert len(rendered.rendered) == 3
    assert fetcher.fetched == []


async def test_the_browser_is_closed_when_the_run_ends(conn: sqlite3.Connection) -> None:
    """실행이 끝나면 닫는다. 실행 사이에 브라우저를 띄워 두지 않는다."""
    workflow_id = add_workflow(conn, "playwright")

    await run_workflow(conn, workflow_id, fetcher=stub_fetcher(), limit=1)

    assert SpyRenderer.built[0].closed is True


async def test_the_browser_is_closed_when_the_run_fails(conn: sqlite3.Connection) -> None:
    """실패한 실행도 종료 경로다. 브라우저가 남으면 다음 실행부터 메모리가 쌓인다."""
    workflow_id = add_workflow(conn, "playwright")
    SpyRenderer.error = TransportError("렌더 실패")

    result = await run_workflow(conn, workflow_id, fetcher=stub_fetcher(), limit=1)

    assert result.status == "failed"
    assert result.error_class == "transport"
    assert SpyRenderer.built[0].closed is True
