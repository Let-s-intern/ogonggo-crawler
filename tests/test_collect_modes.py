"""목록과 상세를 각각 고른 모드로 가져가는지 본다. 섞어 쓰는 조합이 이 시험의 대상이다.

실사이트에도 브라우저에도 나가지 않는다. 정적·API 요청은 `httpx.MockTransport` 를 끼운 진짜
`Fetcher` 가 받고, 렌더 경로는 `Renderer` 자리에 들어간 대역이 받는다. 둘이 각자 기록을
남기므로 어느 요청이 어느 경로로 나갔는지가 갈린다.

API 응답은 2026-08-24 에 받은 LG 응답 픽스처다. HTML 은 이 시험이 만든 최소한의 목록·상세
문서다 — 여기서 보는 것은 파싱이 아니라 어느 경로로 갔는가이고, 파싱은
`tests/test_parser.py` 와 `tests/test_api_source.py` 가 본다.
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
from app.crawler.fetcher import Fetcher, FetchPolicy, FetchResult
from app.crawler.runner import run_workflow

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
LIST_PAYLOAD = (FIXTURES / "lg-list-api-20260824.json").read_text(encoding="utf-8")
DETAIL_PAYLOAD = (FIXTURES / "lg-detail-api-20260824.json").read_text(encoding="utf-8")
API_CONFIG = (FIXTURES / "lg-api-config-20260824.json").read_text(encoding="utf-8")

LIST_URL = "https://careers.lg.com/apply"
# 상세 주소는 `link_template` 이 만드는 것과 같은 모양이어야 한다. 목록이 HTML 인 조합에서
# 상세 API 는 이 주소의 마지막 경로 조각을 id 로 읽는다
DETAIL_URL = "https://careers.lg.com/apply/detail/1002029"
LIST_API = "https://api.careers.lg.com/rmk/job/retrieveJobNoticesList"
DETAIL_API = "https://api.careers.lg.com/rmk/job/retrieveJobNoticesDetail"
ROBOTS = "User-agent: *\nAllow: /\n"

LIST_HTML = f"""
<html><body><ul>
  <li class="job"><a href="{DETAIL_URL}">IT보안 담당자</a><span class="date">2026.08.30</span></li>
</ul></body></html>
"""
DETAIL_HTML = """
<html><body><h1>IT보안 담당자</h1><div class="body">본문이다</div></body></html>
"""

SELECTORS = {
    "list": {"item": "li.job", "title": "a", "link": "a", "date": ".date", "company_name": ""},
    "detail": {
        "title": "h1",
        "body": ".body",
        "qualifications": "",
        "recruitment_end_at": "",
        "department": "",
        "company_name": "",
    },
}


class SpyRenderer:
    """`Renderer` 자리에 들어가는 대역. 브라우저 없이 HTML 을 돌려준다.

    정적 클라이언트에 넘기지 않는다. 그래야 어느 요청이 렌더로 갔는지가 기록으로 갈린다.
    """

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


class RecordingFetcher(Fetcher):
    """나간 요청을 메서드와 함께 적어 둔다. robots 는 셈에서 뺀다."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.calls: list[tuple[str, str]] = []

    async def request(self, url: str, **kwargs: Any) -> FetchResult:
        if not url.endswith("/robots.txt"):
            self.calls.append((str(kwargs.get("method", "GET")), url))
        return await super().request(url, **kwargs)


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


def handle(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/robots.txt":
        return httpx.Response(200, text=ROBOTS)
    if str(request.url) == LIST_API:
        return httpx.Response(200, text=LIST_PAYLOAD)
    if str(request.url) == DETAIL_API:
        return httpx.Response(200, text=DETAIL_PAYLOAD)
    if str(request.url) == LIST_URL:
        return httpx.Response(200, text=LIST_HTML)
    if str(request.url) == DETAIL_URL:
        return httpx.Response(200, text=DETAIL_HTML)
    return httpx.Response(404, text="not found")


def fetcher() -> RecordingFetcher:
    return RecordingFetcher(
        settings=Settings(crawl_delay_seconds=0.0, crawl_max_retries=0),
        transport=httpx.MockTransport(handle),
    )


def add_workflow(conn: sqlite3.Connection, list_mode: str, detail_mode: str) -> int:
    conn.execute(
        """
        INSERT INTO crawlers (name, list_url, selectors_json, status, list_mode, detail_mode,
                              api_config_json)
        VALUES ('LG', ?, ?, 'promoted', ?, ?, ?)
        """,
        (LIST_URL, json.dumps(SELECTORS), list_mode, detail_mode, API_CONFIG),
    )
    cursor = conn.execute(
        "INSERT INTO workflows (crawler_id, name, interval_minutes) VALUES (1, 'LG', 30)"
    )
    return int(cursor.lastrowid or 0)


def stored(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT source_url, raw_data_json FROM raw_jobs ORDER BY id").fetchall()
    return [{"source_url": row["source_url"], **json.loads(row["raw_data_json"])} for row in rows]


async def test_static_list_with_an_api_detail(conn: sqlite3.Connection) -> None:
    """목록은 정적 HTML, 상세는 JSON API. id 는 상세 링크의 마지막 조각에서 온다."""
    workflow_id = add_workflow(conn, "static", "api")
    client = fetcher()

    result = await run_workflow(conn, workflow_id, fetcher=client, limit=1)

    assert result.status == "success"
    assert SpyRenderer.built == []
    assert client.calls == [("GET", LIST_URL), ("POST", DETAIL_API)]
    # 상세는 API 에서 왔다. 본문이 `detailContext` 의 HTML 조각 그대로다
    assert stored(conn)[0]["body"].startswith("<!--StartFragment-->")


async def test_api_list_with_an_api_detail(conn: sqlite3.Connection) -> None:
    """양쪽 다 API. 브라우저가 뜨지 않고 요청 두 번으로 끝난다."""
    workflow_id = add_workflow(conn, "api", "api")
    client = fetcher()

    result = await run_workflow(conn, workflow_id, fetcher=client, limit=1)

    assert result.status == "success"
    assert SpyRenderer.built == []
    assert client.calls == [("POST", LIST_API), ("POST", DETAIL_API)]
    assert result.matched == 83

    row = stored(conn)[0]
    assert row["source_url"] == "https://careers.lg.com/apply/detail?id=1002029"
    assert row["company_name"] == "LG유플러스"


async def test_api_list_with_a_rendered_detail(conn: sqlite3.Connection) -> None:
    """목록은 API, 상세는 렌더. 브라우저는 상세 때문에 한 번만 뜬다."""
    workflow_id = add_workflow(conn, "api", "playwright")
    client = fetcher()

    result = await run_workflow(conn, workflow_id, fetcher=client, limit=1)

    assert result.status == "success"
    # 목록이 브라우저를 띄우지 않았다. 렌더러가 본 URL 은 상세뿐이다
    assert len(SpyRenderer.built) == 1
    assert SpyRenderer.built[0].rendered == ["https://careers.lg.com/apply/detail?id=1002029"]
    assert client.calls == [("POST", LIST_API)]
    assert stored(conn)[0]["body"] == "본문이다"


async def test_rendered_list_and_detail_share_one_browser(conn: sqlite3.Connection) -> None:
    """양쪽 다 렌더. 브라우저 하나를 목록과 상세가 나눠 쓰고 실행이 끝나면 닫힌다."""
    workflow_id = add_workflow(conn, "playwright", "playwright")
    client = fetcher()

    result = await run_workflow(conn, workflow_id, fetcher=client, limit=1)

    assert result.status == "success"
    assert len(SpyRenderer.built) == 1
    assert SpyRenderer.built[0].rendered == [LIST_URL, DETAIL_URL]
    assert SpyRenderer.built[0].closed is True
    # 러너가 직접 가져간 것은 없다
    assert client.calls == []


async def test_an_api_list_that_comes_back_empty_is_a_failure(conn: sqlite3.Connection) -> None:
    """200 인데 배열이 비었다. 신규 0건인 정상 실행으로 남기지 않는다."""
    workflow_id = add_workflow(conn, "api", "api")

    def empty(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        return httpx.Response(200, text=json.dumps({"data": {"jobNoticeList": []}}))

    client = RecordingFetcher(
        settings=Settings(crawl_delay_seconds=0.0, crawl_max_retries=0),
        transport=httpx.MockTransport(empty),
    )

    result = await run_workflow(conn, workflow_id, fetcher=client, limit=1)

    assert result.status == "failed"
    assert result.error_class == "selector_miss"
    assert "빈 배열" in result.error_message
    assert stored(conn) == []


async def test_an_api_crawler_without_selectors_still_runs(conn: sqlite3.Connection) -> None:
    """양쪽 다 API 면 셀렉터를 아무도 읽지 않는다. 없다고 실행을 막지 않는다."""
    workflow_id = add_workflow(conn, "api", "api")
    conn.execute("UPDATE crawlers SET selectors_json = NULL WHERE id = 1")

    result = await run_workflow(conn, workflow_id, fetcher=fetcher(), limit=1)

    assert result.status == "success"


async def test_a_broken_api_config_fails_the_run_with_a_reason(conn: sqlite3.Connection) -> None:
    """설정이 깨져 있으면 실행하지 못한다. 그것도 종료 경로라 행은 남는다."""
    workflow_id = add_workflow(conn, "api", "api")
    conn.execute("UPDATE crawlers SET api_config_json = '{\"list\": {}}' WHERE id = 1")

    result = await run_workflow(conn, workflow_id, fetcher=fetcher(), limit=1)

    assert result.status == "failed"
    # transport·selector_miss·parse 중 어느 것도 아니다. 모르는 채로 두고 사유만 남긴다
    assert result.error_class is None
    assert "API 설정을 읽을 수 없다" in result.error_message
    row = conn.execute("SELECT count(*) AS n FROM crawl_runs").fetchone()
    assert row["n"] == 1
