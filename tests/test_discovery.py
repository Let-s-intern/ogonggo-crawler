"""등록 순서 하나로 묶은 것 테스트.

세 경로(정적·렌더·클릭)를 픽스처로 각각 돌리고, 판정과 **근거 문장**이 서로 다른지 본다.
실사이트에 나가지 않는다 — 목록은 `httpx.MockTransport`, 브라우저는 스텁이다.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.crawler.collect import API
from app.crawler.failures import LIST_EMPTY
from app.crawler.fetcher import Fetcher
from app.crawler.playwright import PLAYWRIGHT, STATIC, ProbeSession, RequestLog
from app.selector.discovery import discover_detail_path
from app.selector.schema import parse_selectors

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
ROBOTS = "User-agent: *\nAllow: /\n"

LIST_URL = "https://example.test/jobs"
SAMSUNG_DETAIL_URL = "https://www.samsungcareers.com/recruit/detail.data?seqno=22878&strCode="
SAMSUNG_BODY = (FIXTURES / "samsung-detail-20260825.json").read_text(encoding="utf-8")

# 정적으로 항목과 상세 주소가 다 있는 목록. 여기서 끝나면 브라우저를 띄우지 않는다
STATIC_LIST = """
<html><body><ul>
  <li class="item"><a href="/jobs/21931885">롯데케미칼 신입 채용</a><span>2026.08.20</span></li>
  <li class="item"><a href="/jobs/21931886">롯데정밀화학 채용</a><span>2026.08.21</span></li>
</ul></body></html>
"""

# 껍데기만 오는 목록. 항목이 없어 렌더로 넘어간다
SHELL = "<html><body><div id='root'></div></body></html>"

# 렌더하면 항목이 생기고 상세 주소도 있는 목록
RENDERED_WITH_LINKS = """
<html><body><ul>
  <li class="item"><a href="https://example.test/jobs/1002099">보건관리자 채용</a></li>
  <li class="item"><a href="https://example.test/jobs/1002100">네트워크 엔지니어</a></li>
</ul></body></html>
"""

# 렌더해도 상세 주소가 없는 목록. 삼성이 이 모양이다
RENDERED_WITHOUT_LINKS = """
<html><body><ul>
  <li class="item"><a href="/#none" data-value="22,878"><p class="tit">2026년 채용</p></a></li>
  <li class="item"><a href="/#none" data-value="22,879"><p class="tit">경력 채용</p></a></li>
</ul></body></html>
"""

LINKED_SELECTORS = parse_selectors(
    json.dumps(
        {
            "list": {"item": "li.item", "title": "a", "link": "a", "date": "span"},
            "detail": {
                "title": "h1",
                "body": "div.body",
                "qualifications": "",
                "recruitment_end_at": "",
                "department": "",
            },
        }
    )
)

# 상세 링크가 없다고 모델이 답한 셀렉터. 클릭으로 알아내야 하는 사이트다
LINKLESS_SELECTORS = parse_selectors(
    json.dumps(
        {
            "list": {"item": "li.item", "title": "p.tit", "link": "", "date": "span.date"},
            "detail": {
                "title": "h1",
                "body": "div.body",
                "qualifications": "",
                "recruitment_end_at": "",
                "department": "",
            },
        }
    )
)


def settings() -> Settings:
    return Settings(crawl_delay_seconds=0.0, crawl_max_retries=1)


def fetcher_for(handler: Any) -> Fetcher:
    return Fetcher(settings=settings(), transport=httpx.MockTransport(handler))


def handler_for(pages: dict[str, str], seen: list[str] | None = None) -> Any:
    """주소마다 정해 둔 응답을 준다. 없는 주소는 404 다."""

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        if seen is not None:
            seen.append(url)
        if url in pages:
            return httpx.Response(
                200, text=pages[url], headers={"content-type": "text/html;charset=utf-8"}
            )
        return httpx.Response(404, text="not found")

    return handle


class StubElement:
    def __init__(self, *, action: Any = None) -> None:
        self.action = action
        self.clicks = 0

    async def get_attribute(self, name: str) -> str | None:
        return None

    async def query_selector_all(self, selector: str) -> list[StubElement]:
        return []

    async def click(self, **kwargs: Any) -> None:
        self.clicks += 1
        if self.action is not None:
            self.action()


class StubPage:
    def __init__(self, html: str, items: list[StubElement]) -> None:
        self.url = LIST_URL
        self.html = html
        self.items = items

    async def query_selector_all(self, selector: str) -> list[StubElement]:
        return list(self.items)

    async def content(self) -> str:
        return self.html


class StubContext:
    def __init__(self, page: StubPage) -> None:
        self.pages = [page]


class StubResponse:
    """브라우저가 받은 응답 하나. 본문은 관찰하는 쪽이 읽어 간다."""

    def __init__(self, url: str, body: str) -> None:
        self.url = url
        self.status = 200
        self.headers = {"content-type": "application/json"}
        self.request = type("StubRequest", (), {"method": "GET", "post_data": None})()
        self._body = body

    async def text(self) -> str:
        return self._body


class StubEmitter:
    """클릭 뒤 나가는 요청을 흘려보낸다."""

    def __init__(self) -> None:
        self.handlers: list[Any] = []

    def on(self, event: str, handler: Any) -> None:
        if event == "response":
            self.handlers.append(handler)

    def emit(self, url: str, body: str) -> None:
        for handler in self.handlers:
            handler(StubResponse(url, body))


async def nosleep(seconds: float) -> None:
    """클릭 뒤 기다리는 척만 한다. 판정에 실제 대기 시간이 필요하지 않다."""
    return None


def opener(session: ProbeSession, opened: list[str]) -> Any:
    @asynccontextmanager
    async def open_probe(url: str) -> Any:
        opened.append(url)
        yield session

    return open_probe


def session_for(html: str, items: list[StubElement], log: RequestLog | None = None) -> ProbeSession:
    page = StubPage(html, items)
    return ProbeSession(
        page=page,
        context=StubContext(page),
        log=log or RequestLog(),
        html=html,
        url=LIST_URL,
    )


@pytest.mark.asyncio
async def test_정적으로_되면_브라우저를_띄우지_않는다() -> None:
    opened: list[str] = []
    client = fetcher_for(handler_for({LIST_URL: STATIC_LIST}))
    try:
        discovery = await discover_detail_path(
            LIST_URL,
            LINKED_SELECTORS,
            fetcher=client,
            sleep=nosleep,
            open_probe=opener(session_for(STATIC_LIST, []), opened),
        )
    finally:
        await client.aclose()

    assert discovery.ok is True
    assert discovery.list_mode == STATIC
    assert discovery.detail_mode == STATIC
    assert opened == []
    assert (
        discovery.evidence
        == "정적 목록에서 항목 2건과 상세 주소를 찾았다. 브라우저를 띄우지 않았다"
    )


@pytest.mark.asyncio
async def test_렌더해서_상세_주소를_얻으면_클릭하지_않는다() -> None:
    opened: list[str] = []
    item = StubElement()
    client = fetcher_for(
        handler_for(
            {
                LIST_URL: SHELL,
                "https://example.test/jobs/1002099": (
                    "<html><body><h1>보건관리자 채용</h1></body></html>"
                ),
            }
        )
    )
    try:
        discovery = await discover_detail_path(
            LIST_URL,
            LINKED_SELECTORS,
            fetcher=client,
            sleep=nosleep,
            open_probe=opener(session_for(RENDERED_WITH_LINKS, [item]), opened),
        )
    finally:
        await client.aclose()

    assert discovery.ok is True
    assert discovery.list_mode == PLAYWRIGHT
    assert discovery.detail_mode == STATIC
    assert opened == [LIST_URL]
    assert item.clicks == 0
    assert "정적 목록에 항목 0건, 렌더 후 2건" in discovery.evidence
    assert "클릭하지 않았다" in discovery.evidence


@pytest.mark.asyncio
async def test_클릭이_상세_API_를_알려_주면_다시_불러_확인하고_채택한다() -> None:
    """삼성. 주소가 그대로여서 요청을 보지 않으면 실패로 읽힌다."""
    opened: list[str] = []
    emitter = StubEmitter()
    log = RequestLog()
    log.attach(emitter)

    def open_modal() -> None:
        emitter.emit(SAMSUNG_DETAIL_URL, SAMSUNG_BODY)

    session = session_for(RENDERED_WITHOUT_LINKS, [StubElement(action=open_modal)], log)
    client = fetcher_for(handler_for({LIST_URL: SHELL, SAMSUNG_DETAIL_URL: SAMSUNG_BODY}))
    try:
        discovery = await discover_detail_path(
            LIST_URL,
            LINKLESS_SELECTORS,
            fetcher=client,
            sleep=nosleep,
            open_probe=opener(session, opened),
        )
    finally:
        await client.aclose()

    assert discovery.ok is True
    assert discovery.detail_mode == API
    assert discovery.detail is not None
    assert discovery.detail.api is not None
    detail = discovery.detail.api.detail
    assert detail is not None
    assert detail.url == "https://www.samsungcareers.com/recruit/detail.data?seqno={id}&strCode="
    assert "클릭했다" in discovery.evidence
    assert "상세 API 를 알려 줬고" in discovery.evidence


@pytest.mark.asyncio
async def test_세_경로가_서로_다른_근거를_남긴다() -> None:
    """판정만 남기면 다음 사람이 왜 그렇게 정해졌는지 알 수 없다."""
    opened: list[str] = []
    static_client = fetcher_for(handler_for({LIST_URL: STATIC_LIST}))
    render_client = fetcher_for(
        handler_for(
            {
                LIST_URL: SHELL,
                "https://example.test/jobs/1002099": (
                    "<html><body><h1>보건관리자 채용</h1></body></html>"
                ),
            }
        )
    )
    emitter = StubEmitter()
    log = RequestLog()
    log.attach(emitter)
    click_client = fetcher_for(handler_for({LIST_URL: SHELL, SAMSUNG_DETAIL_URL: SAMSUNG_BODY}))

    try:
        static = await discover_detail_path(
            LIST_URL,
            LINKED_SELECTORS,
            fetcher=static_client,
            sleep=nosleep,
            open_probe=opener(session_for(STATIC_LIST, []), opened),
        )
        rendered = await discover_detail_path(
            LIST_URL,
            LINKED_SELECTORS,
            fetcher=render_client,
            sleep=nosleep,
            open_probe=opener(session_for(RENDERED_WITH_LINKS, [StubElement()]), opened),
        )
        clicked = await discover_detail_path(
            LIST_URL,
            LINKLESS_SELECTORS,
            fetcher=click_client,
            sleep=nosleep,
            open_probe=opener(
                session_for(
                    RENDERED_WITHOUT_LINKS,
                    [StubElement(action=lambda: emitter.emit(SAMSUNG_DETAIL_URL, SAMSUNG_BODY))],
                    log,
                ),
                opened,
            ),
        )
    finally:
        await static_client.aclose()
        await render_client.aclose()
        await click_client.aclose()

    evidences = {static.evidence, rendered.evidence, clicked.evidence}
    assert len(evidences) == 3
    assert all(evidence for evidence in evidences)


@pytest.mark.asyncio
async def test_클릭해도_아무_일도_없으면_상세_도달_실패다() -> None:
    opened: list[str] = []
    client = fetcher_for(handler_for({LIST_URL: SHELL}))
    try:
        discovery = await discover_detail_path(
            LIST_URL,
            LINKLESS_SELECTORS,
            fetcher=client,
            sleep=nosleep,
            open_probe=opener(session_for(RENDERED_WITHOUT_LINKS, [StubElement()]), opened),
        )
    finally:
        await client.aclose()

    assert discovery.ok is False
    assert discovery.failure == "detail_unreachable"
    assert "클릭했다" in discovery.evidence


@pytest.mark.asyncio
async def test_렌더해도_항목이_0건이면_list_empty_다() -> None:
    opened: list[str] = []
    client = fetcher_for(handler_for({LIST_URL: SHELL}))
    try:
        discovery = await discover_detail_path(
            LIST_URL,
            LINKED_SELECTORS,
            fetcher=client,
            sleep=nosleep,
            open_probe=opener(session_for(SHELL, []), opened),
        )
    finally:
        await client.aclose()

    assert discovery.ok is False
    assert discovery.failure == LIST_EMPTY
    assert "렌더 후에도 0건" in discovery.evidence


@pytest.mark.asyncio
async def test_다시_불러_확인되지_않으면_채택하지_않는다() -> None:
    """브라우저에서만 되는 요청을 저장하면 이후 실행이 전부 실패한다."""
    opened: list[str] = []
    emitter = StubEmitter()
    log = RequestLog()
    log.attach(emitter)
    session = session_for(
        RENDERED_WITHOUT_LINKS,
        [StubElement(action=lambda: emitter.emit(SAMSUNG_DETAIL_URL, SAMSUNG_BODY))],
        log,
    )
    # 다시 부르면 로그인 페이지가 200 으로 온다
    client = fetcher_for(
        handler_for({LIST_URL: SHELL, SAMSUNG_DETAIL_URL: "<html>로그인이 필요합니다</html>"})
    )
    try:
        discovery = await discover_detail_path(
            LIST_URL,
            LINKLESS_SELECTORS,
            fetcher=client,
            sleep=nosleep,
            open_probe=opener(session, opened),
        )
    finally:
        await client.aclose()

    assert discovery.ok is False
    assert discovery.detail_mode == ""
    assert "다시 불러 확인하지 못했다" in discovery.reason


@pytest.mark.asyncio
async def test_브라우저를_열_수_없으면_사유를_남긴다() -> None:
    client = fetcher_for(handler_for({LIST_URL: SHELL}))
    try:
        discovery = await discover_detail_path(LIST_URL, LINKED_SELECTORS, fetcher=client)
    finally:
        await client.aclose()

    assert discovery.ok is False
    assert discovery.failure == LIST_EMPTY
    assert "브라우저를 열 수 없다" in discovery.reason


# 렌더 중 페이지가 목록을 JSON 으로 받아 그린 경우. 카카오·우아한형제들이 이 모양이다
LIST_API_URL = "https://example.test/api/job-list?page=1"
LIST_API_BODY = json.dumps(
    {
        "data": {
            "list": [
                {"jobId": "1002099", "name": "보건관리자 채용"},
                {"jobId": "1002100", "name": "네트워크 엔지니어"},
            ]
        }
    },
    ensure_ascii=False,
)


@pytest.mark.asyncio
async def test_렌더_중_관찰한_목록_API_를_다시_불러_확인하고_채택한다() -> None:
    """목록이 API 로 오면 실행마다 브라우저를 띄우지 않는다."""
    opened: list[str] = []
    emitter = StubEmitter()
    log = RequestLog()
    log.attach(emitter)
    emitter.emit(LIST_API_URL, LIST_API_BODY)
    # 실제 경로에서는 `open_probe` 가 페이지를 넘기기 전에 본문 읽기를 기다린다
    await log.drain()

    client = fetcher_for(
        handler_for(
            {
                LIST_URL: SHELL,
                LIST_API_URL: LIST_API_BODY,
                "https://example.test/jobs/1002099": (
                    "<html><body><h1>보건관리자 채용</h1></body></html>"
                ),
            }
        )
    )
    try:
        discovery = await discover_detail_path(
            LIST_URL,
            LINKED_SELECTORS,
            fetcher=client,
            sleep=nosleep,
            open_probe=opener(session_for(RENDERED_WITH_LINKS, [StubElement()], log), opened),
        )
    finally:
        await client.aclose()

    assert discovery.ok is True
    assert discovery.list_mode == API
    assert discovery.detail_mode == STATIC
    assert discovery.list_adopted is True
    assert discovery.list is not None
    config = discovery.list.config()
    assert config.items_path == "data.list"
    assert config.fields["title"] == "name"
    assert config.id_field == "jobId"
    assert config.link_template == "https://example.test/jobs/{id}"
    assert "목록은 https://example.test/api/job-list?page=1 의 `data.list` 로 온다" in (
        discovery.evidence
    )


@pytest.mark.asyncio
async def test_목록_API_가_없으면_렌더_그대로_남는다() -> None:
    """토스. 공고는 초기 HTML 에 있고 렌더 중 나간 JSON 에는 목록이 없다."""
    opened: list[str] = []
    emitter = StubEmitter()
    log = RequestLog()
    log.attach(emitter)
    emitter.emit("https://example.test/api/banner", json.dumps({"items": [{"title": "배너"}]}))
    await log.drain()

    client = fetcher_for(
        handler_for(
            {
                LIST_URL: SHELL,
                "https://example.test/jobs/1002099": (
                    "<html><body><h1>보건관리자 채용</h1></body></html>"
                ),
            }
        )
    )
    try:
        discovery = await discover_detail_path(
            LIST_URL,
            LINKED_SELECTORS,
            fetcher=client,
            sleep=nosleep,
            open_probe=opener(session_for(RENDERED_WITH_LINKS, [StubElement()], log), opened),
        )
    finally:
        await client.aclose()

    assert discovery.ok is True
    assert discovery.list_mode == PLAYWRIGHT
    assert discovery.list_adopted is False
    assert "목록 API 는 찾지 못했다" in discovery.evidence


@pytest.mark.asyncio
async def test_브라우저에서만_되는_목록_API_는_채택하지_않는다() -> None:
    """다시 불러 확인되지 않으면 렌더 경로로 남는다. 저장하면 이후 실행이 전부 실패한다."""
    opened: list[str] = []
    emitter = StubEmitter()
    log = RequestLog()
    log.attach(emitter)
    emitter.emit(LIST_API_URL, LIST_API_BODY)
    # 실제 경로에서는 `open_probe` 가 페이지를 넘기기 전에 본문 읽기를 기다린다
    await log.drain()

    client = fetcher_for(
        handler_for(
            {
                LIST_URL: SHELL,
                # 목록 API 주소는 응답하지 않는다. 브라우저에서만 되는 요청과 같은 모양이다
                "https://example.test/jobs/1002099": (
                    "<html><body><h1>보건관리자 채용</h1></body></html>"
                ),
            }
        )
    )
    try:
        discovery = await discover_detail_path(
            LIST_URL,
            LINKED_SELECTORS,
            fetcher=client,
            sleep=nosleep,
            open_probe=opener(session_for(RENDERED_WITH_LINKS, [StubElement()], log), opened),
        )
    finally:
        await client.aclose()

    assert discovery.list_mode == PLAYWRIGHT
    assert discovery.list_adopted is False
    assert "채택하지 않았다" in discovery.evidence


# 항목에 `href` 가 없고 공고 번호가 `onclick` 인자에만 있는 목록. 두산·네이버가 이 모양이다
ONCLICK_LIST = """
<html><body><ul>
  <li class="item"><a class="tit" href="javascript:void(0);" onclick="show('30005276')">
    <p class="tit">온보딩 프로그램 운영 지원</p></a></li>
  <li class="item"><a class="tit" href="javascript:void(0);" onclick="show('30005281')">
    <p class="tit">재무회계 담당</p></a></li>
</ul></body></html>
"""
VIEW_URL = "https://example.test/rcrt/view.do?annoId=30005276&lang=ko"


@pytest.mark.asyncio
async def test_클릭이_데려간_주소를_공고마다_다른_형식으로_저장한다() -> None:
    """주소 하나만 저장하면 공고가 몇 건이든 같은 상세를 가져온다."""
    opened: list[str] = []
    page_html = "<html><body><h1>온보딩 프로그램 운영 지원</h1></body></html>"
    second = "<html><body><h1>재무회계 담당</h1></body></html>"

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        if request.url.path == "/jobs":
            return httpx.Response(200, text=SHELL)
        anno = request.url.params.get("annoId", "")
        text = page_html if anno == "30005276" else second
        return httpx.Response(200, text=text)

    client = fetcher_for(handle)
    element = StubElement()
    session = session_for(ONCLICK_LIST, [element])
    # 클릭이 상세 주소로 데려갔다
    element.action = lambda: setattr(session.page, "url", VIEW_URL)
    try:
        discovery = await discover_detail_path(
            LIST_URL,
            LINKLESS_SELECTORS,
            fetcher=client,
            sleep=nosleep,
            open_probe=opener(session, opened),
        )
    finally:
        await client.aclose()

    assert discovery.ok is True
    assert discovery.detail_mode == STATIC
    assert discovery.link is not None
    assert discovery.link.selector == "a.tit"
    assert (
        discovery.link.template == "https://example.test/rcrt/view.do?annoId={onclick|arg1}&lang=ko"
    )
    assert (discovery.link.resolved, discovery.link.count) == (2, 2)
    # 상세 셀렉터를 만들 때 볼 페이지다. 클릭해도 주소가 그대로인 사이트에서도 이 값은
    # 공고 하나의 주소다
    assert discovery.link.sample == "https://example.test/rcrt/view.do?annoId=30005276&lang=ko"
    assert discovery.detail is not None
    assert discovery.detail.url == discovery.link.sample
    assert "공고마다 다른 상세 주소 형식을 얻었다" in discovery.evidence


@pytest.mark.asyncio
async def test_확인되지_않는_주소_형식은_채택하지_않는다() -> None:
    """두 번째 항목이 첫 항목과 같은 페이지를 주면 그 형식은 공고를 가르지 못한다."""
    opened: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        if request.url.path == "/jobs":
            return httpx.Response(200, text=SHELL)
        return httpx.Response(200, text="<html><h1>온보딩 프로그램 운영 지원</h1></html>")

    client = fetcher_for(handle)
    element = StubElement()
    session = session_for(ONCLICK_LIST, [element])
    element.action = lambda: setattr(session.page, "url", VIEW_URL)
    try:
        discovery = await discover_detail_path(
            LIST_URL,
            LINKLESS_SELECTORS,
            fetcher=client,
            sleep=nosleep,
            open_probe=opener(session, opened),
        )
    finally:
        await client.aclose()

    assert discovery.link is None
    assert "채택하지 않았다" in discovery.evidence


def guarded_opener(client: Fetcher, session: ProbeSession, opened: list[str]) -> Any:
    """실제 렌더러처럼 호스트 잠금을 잡은 채로 페이지를 내주는 opener.

    `Renderer.open_probe` 가 `Fetcher.guard()` 안에서 페이지를 연다. 그 안에서 같은 호스트로
    정적 요청을 내면 자기 잠금을 기다리게 되므로, 확인은 브라우저를 닫은 뒤에 해야 한다.
    """

    @asynccontextmanager
    async def open_probe(url: str) -> Any:
        opened.append(url)
        async with client.guard(url):
            yield session

    return open_probe


@pytest.mark.asyncio
async def test_확인은_브라우저를_닫은_뒤에_한다() -> None:
    """렌더가 호스트 잠금을 잡은 채로 같은 호스트를 다시 부르면 영영 멈춘다."""
    opened: list[str] = []
    client = fetcher_for(
        handler_for(
            {
                LIST_URL: SHELL,
                "https://example.test/jobs/1002099": (
                    "<html><body><h1>보건관리자 채용</h1></body></html>"
                ),
            }
        )
    )
    try:
        async with asyncio.timeout(5):
            discovery = await discover_detail_path(
                LIST_URL,
                LINKED_SELECTORS,
                fetcher=client,
                sleep=nosleep,
                open_probe=guarded_opener(
                    client, session_for(RENDERED_WITH_LINKS, [StubElement()]), opened
                ),
            )
    finally:
        await client.aclose()

    assert discovery.ok is True
    assert discovery.detail_mode == STATIC
    assert opened == [LIST_URL]


@pytest.mark.asyncio
async def test_주소_형식을_알고_나면_목록을_정적으로_둔다() -> None:
    """두산·네이버. 항목은 정적으로 다 있고 상세 주소만 `onclick` 에 있었다."""
    opened: list[str] = []
    page_html = "<html><body><h1>온보딩 프로그램 운영 지원</h1></body></html>"
    second = "<html><body><h1>재무회계 담당</h1></body></html>"

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        if request.url.path == "/jobs":
            # 정적으로도 목록이 다 온다. 없던 것은 상세 주소뿐이다
            return httpx.Response(200, text=ONCLICK_LIST)
        anno = request.url.params.get("annoId", "")
        return httpx.Response(200, text=page_html if anno == "30005276" else second)

    client = fetcher_for(handle)
    element = StubElement()
    session = session_for(ONCLICK_LIST, [element])
    element.action = lambda: setattr(session.page, "url", VIEW_URL)
    try:
        discovery = await discover_detail_path(
            LIST_URL,
            LINKLESS_SELECTORS,
            fetcher=client,
            sleep=nosleep,
            open_probe=opener(session, opened),
        )
    finally:
        await client.aclose()

    assert discovery.ok is True
    assert discovery.list_mode == STATIC
    assert discovery.detail_mode == STATIC
    assert "목록을 정적으로 둔다" in discovery.evidence
