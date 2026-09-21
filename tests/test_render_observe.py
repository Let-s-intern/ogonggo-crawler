"""렌더 중 요청 관찰 테스트.

브라우저를 띄우지 않는다. 응답 이벤트를 흉내 내는 스텁 페이지가 자산·분석 도구·데이터 요청을
섞어 내보내고, 남는 것이 데이터 요청뿐인지 본다 (`tests/test_render.py` 와 같은 방식).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.config import Settings
from app.crawler.fetcher import Fetcher
from app.crawler.playwright import (
    OBSERVED_BODY_LIMIT,
    ObservedRequest,
    Renderer,
    RequestLog,
    functional_headers,
    is_data_request,
)

USER_AGENT = "job-crawler-automation (contact: test@example.test)"
LIST_URL = "https://www.samsungcareers.com/hr/"
DETAIL_DATA_URL = "https://www.samsungcareers.com/recruit/detail.data?seqno=22878&strCode="
ROBOTS_ALLOW_ALL = "User-agent: *\nDisallow:\n"

# 한 페이지가 실제로 내는 요청의 축소판. 자산과 분석 도구가 데이터 요청보다 많다
PAGE_TRAFFIC: tuple[dict[str, Any], ...] = (
    {"url": "https://www.samsungcareers.com/hr/", "type": "text/html", "body": "<html></html>"},
    {"url": "https://www.samsungcareers.com/js/app.js", "type": "application/javascript"},
    {"url": "https://www.samsungcareers.com/css/main.css", "type": "text/css"},
    {"url": "https://www.samsungcareers.com/img/logo.png", "type": "image/png"},
    {"url": "https://www.samsungcareers.com/fonts/noto.woff2", "type": "font/woff2"},
    {
        "url": "https://www.googletagmanager.com/gtm.js?id=GTM-XXXX",
        "type": "application/javascript",
    },
    {"url": "https://www.google-analytics.com/collect?v=2", "type": "text/plain"},
    {"url": "https://connect.facebook.net/en_US/fbevents.js", "type": "application/javascript"},
    {
        "url": "https://www.samsungcareers.com/hr/list.data",
        "type": "text/html;charset=utf-8",
        "method": "POST",
        "post_data": "currentPageNo=1&intNo=0",
        "body": '<ul><li class="list"><a data-value="22,878">공고</a></li></ul>',
    },
    {
        "url": DETAIL_DATA_URL,
        "type": "application/json;charset=utf-8",
        "body": '{"data":{"result":{"title":"공고 하나"}}}',
    },
    # 확장자가 없는 자산. content-type 으로만 걸러진다
    {"url": "https://www.samsungcareers.com/media/hero", "type": "image/jpeg"},
)


class StubResponse:
    def __init__(self, entry: dict[str, Any]) -> None:
        self.url = str(entry["url"])
        self.status = int(entry.get("status", 200))
        self.headers = {"content-type": str(entry.get("type", ""))}
        self._body = str(entry.get("body", ""))
        self.request = type(
            "StubRequest",
            (),
            {
                "method": str(entry.get("method", "GET")),
                "post_data": entry.get("post_data"),
                "headers": entry.get("request_headers", {}),
            },
        )()
        self.read_count = 0

    async def text(self) -> str:
        self.read_count += 1
        return self._body


class StubPage:
    """`page.on("response", ...)` 를 흉내 낸다. `goto` 가 트래픽을 흘려보낸다."""

    def __init__(self, traffic: tuple[dict[str, Any], ...], html: str) -> None:
        self.url = ""
        self.traffic = traffic
        self._html = html
        self._handlers: list[Any] = []
        self.responses: list[StubResponse] = []

    def on(self, event: str, handler: Any) -> None:
        if event == "response":
            self._handlers.append(handler)

    async def goto(self, url: str, **kwargs: Any) -> Any:
        self.url = url
        for entry in self.traffic:
            response = StubResponse(entry)
            self.responses.append(response)
            for handler in self._handlers:
                handler(response)
        return type("Response", (), {"status": 200})()

    async def wait_for_load_state(self, state: str, **kwargs: Any) -> None:
        return None

    async def wait_for_function(self, expression: str, **kwargs: Any) -> None:
        return None

    async def content(self) -> str:
        return self._html


class StubContext:
    def __init__(self, page: StubPage) -> None:
        self._page = page
        self.closed = False

    async def new_page(self) -> StubPage:
        return self._page

    async def close(self) -> None:
        self.closed = True


class StubBrowser:
    def __init__(self, page: StubPage) -> None:
        self._page = page
        self.closed = False

    async def new_context(self, *, user_agent: str) -> StubContext:
        return StubContext(self._page)

    async def close(self) -> None:
        self.closed = True

    async def stop(self) -> None:
        return None


def make_fetcher() -> Fetcher:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        return httpx.Response(200, text="")

    settings = Settings(crawl_user_agent=USER_AGENT, crawl_delay_seconds=0.0)
    return Fetcher(settings=settings, transport=httpx.MockTransport(handler))


async def collect(traffic: tuple[dict[str, Any], ...] = PAGE_TRAFFIC) -> list[ObservedRequest]:
    page = StubPage(traffic, "<html><body>목록</body></html>")
    browser = StubBrowser(page)
    fetcher = make_fetcher()
    renderer = Renderer(fetcher, launch=lambda: _launched(browser))
    try:
        _, observed = await renderer.fetch_observed(LIST_URL)
        return observed
    finally:
        await renderer.aclose()
        await fetcher.aclose()


async def _launched(browser: StubBrowser) -> tuple[Any, Any]:
    return browser, browser


@pytest.mark.asyncio
async def test_자산과_분석도구는_기록하지_않는다() -> None:
    observed = await collect()

    urls = [entry.url for entry in observed]
    assert not [url for url in urls if url.endswith((".js", ".css", ".png", ".woff2"))]
    assert not [url for url in urls if "google" in url or "facebook" in url]


@pytest.mark.asyncio
async def test_데이터_요청은_본문까지_남는다() -> None:
    observed = await collect()

    detail = [entry for entry in observed if entry.url == DETAIL_DATA_URL]
    assert len(detail) == 1
    assert detail[0].is_json
    assert "공고 하나" in detail[0].body
    assert detail[0].status == 200

    posted = [entry for entry in observed if entry.method == "POST"]
    assert len(posted) == 1
    assert posted[0].request_body == "currentPageNo=1&intNo=0"
    assert "22,878" in posted[0].body


@pytest.mark.asyncio
async def test_확장자가_없는_자산은_content_type_으로_걸러진다() -> None:
    observed = await collect()

    assert not [entry for entry in observed if entry.url.endswith("/media/hero")]


@pytest.mark.asyncio
async def test_문서_요청은_남는다() -> None:
    """롯데·SK 는 상세가 HTML 문서다. 문서 요청까지 거르면 그 경로를 찾을 수 없다."""
    observed = await collect()

    assert [entry for entry in observed if entry.url == LIST_URL]


@pytest.mark.asyncio
async def test_기록_순서는_응답이_온_순서다() -> None:
    observed = await collect()

    assert [entry.url for entry in observed] == [
        LIST_URL,
        "https://www.samsungcareers.com/hr/list.data",
        DETAIL_DATA_URL,
    ]


@pytest.mark.asyncio
async def test_큰_응답은_상한까지만_들고_있는다() -> None:
    big = (
        {"url": "https://example.test/api/detail", "type": "application/json", "body": "x" * 50},
    )
    page = StubPage(big, "<html></html>")
    log = RequestLog(body_limit=10)
    log.attach(page)
    await page.goto("https://example.test/api/detail")
    await log.drain()

    assert len(log.requests) == 1
    assert log.requests[0].body == "x" * 10
    assert log.requests[0].truncated is True


@pytest.mark.asyncio
async def test_기록_건수에_상한이_있다() -> None:
    traffic = tuple(
        {"url": f"https://example.test/api/{index}", "type": "application/json", "body": "{}"}
        for index in range(10)
    )
    page = StubPage(traffic, "<html></html>")
    log = RequestLog(limit=3)
    log.attach(page)
    await page.goto("https://example.test/api/0")
    await log.drain()

    assert len(log.requests) == 3


@pytest.mark.asyncio
async def test_클릭_전후를_표시로_가른다() -> None:
    page = StubPage(
        ({"url": "https://example.test/api/list", "type": "application/json", "body": "{}"},),
        "<html></html>",
    )
    log = RequestLog()
    log.attach(page)
    await page.goto("https://example.test/")
    await log.drain()

    mark = log.mark()
    page.traffic = (
        {"url": "https://example.test/api/detail", "type": "application/json", "body": "{}"},
    )
    await page.goto("https://example.test/")
    await log.drain()

    assert [entry.url for entry in log.since(mark)] == ["https://example.test/api/detail"]
    assert len(log.requests) == 2


def test_기본_본문_상한은_설정으로_남아_있다() -> None:
    assert OBSERVED_BODY_LIMIT == 200_000


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.test/api/detail.data?seqno=1", True),
        ("https://example.test/", True),
        ("https://example.test/static/app.js", False),
        ("https://example.test/style.css", False),
        ("https://stats.g.doubleclick.net/j/collect", False),
        ("data:image/png;base64,AAAA", False),
        ("blob:https://example.test/abc", False),
    ],
)
def test_기록_대상_판정(url: str, expected: bool) -> None:
    assert is_data_request(url) is expected


@pytest.mark.asyncio
async def test_사이트가_요구하는_헤더만_적는다() -> None:
    """HD현대 목록 API 는 `x-user-role` 이 없으면 500 이다. 그 한 줄이 판정을 가른다."""
    page = StubPage(
        (
            {
                "url": "https://recruit.hd.com/api/v1/jobda/getRecruitNoticeList?isPost=true",
                "type": "application/json",
                "body": '{"data":[]}',
                "request_headers": {
                    "x-user-role": "FRONT",
                    "accept": "application/json, text/plain, */*",
                    "accept-language": "ko",
                    "referer": "https://recruit.hd.com/kr/mainLayout/apply",
                    "sec-fetch-mode": "cors",
                    "user-agent": "Mozilla/5.0",
                    "cookie": "JSESSIONID=abc",
                },
            },
        ),
        "<html></html>",
    )
    log = RequestLog()
    log.attach(page)
    await page.goto("https://recruit.hd.com/kr/mainLayout/apply")
    await log.drain()

    # 브라우저가 스스로 붙인 것은 전부 떨어진다. `cookie` 는 그 브라우저 한 번의 신원이고,
    # 저장하면 등록한 날만 되는 크롤러가 남는다
    assert dict(log.requests[0].request_headers) == {"x-user-role": "FRONT"}


def test_헤더를_읽을_수_없으면_비운다() -> None:
    """playwright 버전에 따라 속성이 없을 수 있다. 그때도 관찰은 계속된다."""
    assert functional_headers(None) == {}
    assert functional_headers("x-user-role: FRONT") == {}


def test_긴_값은_기능성_헤더로_보지_않는다() -> None:
    """토큰이 그 자리에 온다. 저장해도 다음 실행에는 이미 만료돼 있다."""
    assert functional_headers({"x-token": "a" * 400}) == {}
