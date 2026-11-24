"""클릭 판정 테스트.

브라우저를 띄우지 않는다. 여섯 사이트에서 측정된 네 가지 반응(같은 탭 이동, 새 탭, 요청,
모달로 본문 증가)을 스텁 페이지로 재현하고, 각각 도달로 판정하는지 본다.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.crawler.click_probe import (
    DETAIL_UNREACHABLE,
    SIGNAL_BODY,
    SIGNAL_REQUEST,
    SIGNAL_TAB,
    SIGNAL_URL,
    ClickOutcome,
    is_share_link,
    probe_click,
)
from app.crawler.failures import LIST_EMPTY
from app.crawler.playwright import RequestLog

LIST_URL = "https://example.test/jobs"
DETAIL_URL = "https://example.test/jobs/21931885"
SHORT_BODY = "<html><body>목록</body></html>"
LONG_BODY = "<html><body>" + "상세 본문 " * 400 + "</body></html>"


async def nosleep(seconds: float) -> None:
    """기다리는 척만 한다. 클릭 판정에 실제 대기 시간이 필요하지 않다."""
    return None


class StubElement:
    """클릭 대상. 눌리면 등록된 동작이 일어난다."""

    def __init__(
        self,
        *,
        href: str = "",
        onclick: str = "",
        anchors: list[StubElement] | None = None,
        action: Any = None,
    ) -> None:
        self._attributes = {"href": href, "onclick": onclick}
        self._anchors = anchors or []
        self._action = action
        self.clicks = 0

    async def get_attribute(self, name: str) -> str | None:
        return self._attributes.get(name) or None

    async def query_selector_all(self, selector: str) -> list[StubElement]:
        return list(self._anchors) if selector == "a" else []

    async def click(self, **kwargs: Any) -> None:
        self.clicks += 1
        if self._action is not None:
            self._action()


class StubTab:
    def __init__(self, url: str, html: str) -> None:
        self.url = url
        self._html = html
        self.closed = False

    async def content(self) -> str:
        return self._html

    async def close(self) -> None:
        self.closed = True


class StubListPage:
    """목록 페이지. 항목은 회차마다 다르게 줄 수 있다 — 늦게 채우는 사이트를 흉내 낸다."""

    def __init__(
        self,
        items: list[StubElement],
        *,
        html: str = SHORT_BODY,
        appear_after: int = 0,
    ) -> None:
        self.url = LIST_URL
        self.html = html
        self.items = items
        self._appear_after = appear_after
        self.queries = 0

    async def query_selector_all(self, selector: str) -> list[StubElement]:
        self.queries += 1
        if self.queries <= self._appear_after:
            return []
        return list(self.items)

    async def content(self) -> str:
        return self.html


class StubContext:
    def __init__(self, page: Any) -> None:
        self.pages = [page]


def make_log(page: Any) -> RequestLog:
    log = RequestLog()
    log.attach(page)
    return log


class StubEmitter:
    """요청을 흘려보내는 자리. 클릭 동작이 이것을 부른다."""

    def __init__(self) -> None:
        self._handlers: list[Any] = []

    def on(self, event: str, handler: Any) -> None:
        if event == "response":
            self._handlers.append(handler)

    def emit(self, url: str, body: str) -> None:
        response = type(
            "StubResponse",
            (),
            {
                "url": url,
                "status": 200,
                "headers": {"content-type": "application/json"},
                "request": type("StubRequest", (), {"method": "GET", "post_data": None})(),
                "text": _returning(body),
            },
        )()
        for handler in self._handlers:
            handler(response)


def _returning(body: str) -> Any:
    async def text() -> str:
        return body

    return text


async def run(page: Any, context: Any, log: RequestLog, selector: str = "li") -> ClickOutcome:
    return await probe_click(
        page,
        context=context,
        log=log,
        item_selector=selector,
        settle_seconds=0.0,
        sleep=nosleep,
    )


@pytest.mark.asyncio
async def test_같은_탭_이동은_도달이다() -> None:
    """롯데·LG·한화. 클릭하면 주소가 상세로 바뀐다."""
    page = StubListPage([])

    def navigate() -> None:
        page.url = DETAIL_URL
        page.html = LONG_BODY

    item = StubElement(action=navigate)
    page.items = [item]
    context = StubContext(page)

    outcome = await run(page, context, RequestLog())

    assert outcome.reached is True
    assert SIGNAL_URL in outcome.signals
    assert outcome.url == DETAIL_URL
    assert outcome.failure == ""


@pytest.mark.asyncio
async def test_새_탭은_도달이고_읽은_뒤_닫는다() -> None:
    """SK. 목록 탭은 그대로 있고 상세가 새 탭에서 열린다."""
    page = StubListPage([])
    tab = StubTab(DETAIL_URL, LONG_BODY)
    context = StubContext(page)

    item = StubElement(action=lambda: context.pages.append(tab))
    page.items = [item]

    outcome = await run(page, context, RequestLog())

    assert outcome.reached is True
    assert SIGNAL_TAB in outcome.signals
    assert outcome.url == DETAIL_URL
    assert "상세 본문" in outcome.html
    assert tab.closed is True


@pytest.mark.asyncio
async def test_주소가_그대로여도_요청이_나가면_도달이다() -> None:
    """삼성. 모달이라 주소가 바뀌지 않는다. 주소만 보면 실패로 읽힌다."""
    emitter = StubEmitter()
    page = StubListPage([])
    log = make_log(emitter)

    def open_modal() -> None:
        emitter.emit(
            "https://www.samsungcareers.com/recruit/detail.data?seqno=22878&strCode=",
            '{"data":{"result":{"title":"공고"}}}',
        )

    page.items = [StubElement(action=open_modal)]
    context = StubContext(page)

    outcome = await run(page, context, log)

    assert outcome.reached is True
    assert SIGNAL_REQUEST in outcome.signals
    assert outcome.url == LIST_URL
    assert [request.url for request in outcome.requests] == [
        "https://www.samsungcareers.com/recruit/detail.data?seqno=22878&strCode="
    ]


@pytest.mark.asyncio
async def test_본문이_길어지면_도달이다() -> None:
    """요청이 이미 받아 둔 값으로 그려지는 모달. 주소도 요청도 없이 본문만 는다."""
    page = StubListPage([])

    def grow() -> None:
        page.html = LONG_BODY

    page.items = [StubElement(action=grow)]
    context = StubContext(page)

    outcome = await run(page, context, RequestLog())

    assert outcome.reached is True
    assert outcome.signals == (SIGNAL_BODY,)


@pytest.mark.asyncio
async def test_넷_다_없으면_상세_도달_실패다() -> None:
    page = StubListPage([StubElement()])
    context = StubContext(page)

    outcome = await run(page, context, RequestLog())

    assert outcome.reached is False
    assert outcome.failure == DETAIL_UNREACHABLE
    assert "넷 다 그대로다" in outcome.reason


@pytest.mark.asyncio
async def test_항목이_0건이면_클릭_실패가_아니라_list_empty_다() -> None:
    page = StubListPage([], appear_after=1000)
    context = StubContext(page)

    outcome = await run(page, context, RequestLog())

    assert outcome.reached is False
    assert outcome.failure == LIST_EMPTY
    assert "누를 항목이 없다" in outcome.reason


@pytest.mark.asyncio
async def test_목록이_늦게_채워져도_기다렸다_누른다() -> None:
    """삼성은 목록이 늦게 뜬다. 첫 조회가 0건이라고 바로 실패로 보지 않는다."""
    page = StubListPage([], appear_after=3)

    def navigate() -> None:
        page.url = DETAIL_URL

    page.items = [StubElement(action=navigate)]
    context = StubContext(page)

    outcome = await run(page, context, RequestLog())

    assert outcome.reached is True
    assert outcome.item_count == 1
    assert page.queries == 4


@pytest.mark.asyncio
async def test_SNS_공유_버튼은_누르지_않는다() -> None:
    """현대. 항목의 첫 a 들이 공유 버튼이라 누르면 아무 일도 일어나지 않는다."""
    facebook = StubElement(href="javascript:;", onclick="shareSns('facebook', 'x')")
    twitter = StubElement(href="javascript:;", onclick="shareSns('twitter', 'x')")
    page = StubListPage([])
    page.items = [StubElement(anchors=[facebook, twitter])]
    context = StubContext(page)

    outcome = await run(page, context, RequestLog())

    assert outcome.reached is False
    assert facebook.clicks == 0
    assert twitter.clicks == 0
    assert len(outcome.skipped) == 2


@pytest.mark.asyncio
async def test_컨테이너가_안_되면_항목_안의_a_를_누른다() -> None:
    page = StubListPage([])

    def navigate() -> None:
        page.url = DETAIL_URL

    anchor = StubElement(href="/jobs/21931885", action=navigate)
    container = StubElement(anchors=[anchor])
    page.items = [container]
    context = StubContext(page)

    outcome = await run(page, context, RequestLog())

    assert outcome.reached is True
    assert container.clicks == 1
    assert anchor.clicks == 1
    assert outcome.target.startswith("a[0]")


@pytest.mark.parametrize(
    ("href", "onclick", "expected"),
    [
        ("javascript:;", "shareSns('facebook', 'x')", True),
        ("javascript:void(0)", "", True),
        ("#none", "openShareLayer()", True),
        ("/#none", "goDetail(22878)", False),
        ("/jobs/21931885", "", False),
        # 공고를 여는 함수를 부르는 `javascript:` 주소다 (크라운해태, 동화약품)
        ("javascript:view('00702');", "", False),
        ("javascript:view(141);", "", False),
        ("javascript:void(0);", "", True),
    ],
)
def test_누르지_않을_링크_판정(href: str, onclick: str, expected: bool) -> None:
    assert is_share_link(href, onclick) is expected
