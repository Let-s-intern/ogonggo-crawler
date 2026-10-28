"""렌더 경로 테스트.

브라우저를 띄우지 않는다. Playwright 자리에는 호출을 기록하는 스텁이 들어가고, robots 응답은
`httpx.MockTransport` 스텁이다. 시간은 sleep 이 불릴 때만 흐르는 가짜 시계라 실제로 기다리는
구간이 없다 (`tests/test_fetcher.py` 와 같은 방식).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.crawler.fetcher import BotChallengeError, Fetcher, RobotsDisallowedError
from app.crawler.playwright import (
    PLAYWRIGHT,
    STATIC,
    InFlight,
    Renderer,
    RenderError,
    _settle,
    open_source,
)

USER_AGENT = "ogonggo-ai (contact: test@example.test)"
DELAY_SECONDS = 3.0
CHALLENGE_HEADERS = {"x-vercel-mitigated": "challenge"}

LIST_URL = "https://example.test/jobs"
OTHER_URL = "https://example.test/jobs/2"
RENDERED_HTML = "<html><body><ul><li>공고 1</li></ul></body></html>"

ROBOTS_ALLOW_ALL = "User-agent: *\nDisallow:\n"
ROBOTS_DISALLOW_JOBS = "User-agent: *\nDisallow: /jobs\n"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class StubPage:
    def __init__(self, browser: StubBrowser, user_agent: str | None) -> None:
        self._browser = browser
        self._user_agent = user_agent
        self.url = ""

    async def goto(self, url: str, **kwargs: Any) -> Any:
        self._browser.visited.append((self._browser.clock.time(), url))
        self.url = url
        if self._browser.hang:
            await asyncio.sleep(3600)
        user_agent = self._user_agent
        if user_agent in self._browser.challenged:
            return type("Response", (), {"status": 429, "headers": CHALLENGE_HEADERS})()
        return type("Response", (), {"status": self._browser.status, "headers": {}})()

    async def wait_for_load_state(self, state: str, **kwargs: Any) -> None:
        return None

    async def content(self) -> str:
        return self._browser.html


class StubContext:
    def __init__(self, browser: StubBrowser, user_agent: str | None) -> None:
        self._browser = browser
        self.user_agent = user_agent
        self.closed = False

    async def new_page(self) -> StubPage:
        return StubPage(self._browser, self.user_agent)

    async def close(self) -> None:
        self.closed = True


class StubBrowser:
    """띄운 횟수, 방문한 URL, User-Agent, 닫혔는지를 기록한다."""

    def __init__(
        self,
        clock: FakeClock,
        *,
        html: str = RENDERED_HTML,
        status: int = 200,
        hang: bool = False,
        challenged: tuple[str | None, ...] = (),
    ) -> None:
        self.clock = clock
        # 이 User-Agent 로 연 페이지는 봇 확인 챌린지(429)를 받는다
        self.challenged = challenged
        self.html = html
        self.status = status
        self.hang = hang
        self.visited: list[tuple[float, str]] = []
        self.user_agents: list[str | None] = []
        self.contexts: list[StubContext] = []
        self.closed = False
        self.stopped = False

    async def new_context(self, *, user_agent: str | None) -> StubContext:
        self.user_agents.append(user_agent)
        context = StubContext(self, user_agent)
        self.contexts.append(context)
        return context

    async def close(self) -> None:
        self.closed = True

    async def stop(self) -> None:
        self.stopped = True

    def urls(self) -> list[str]:
        return [url for _, url in self.visited]


class StubLauncher:
    """브라우저를 몇 번 띄웠는지 센다. 재사용을 확인하는 수단이다."""

    def __init__(self, browser: StubBrowser) -> None:
        self.browser = browser
        self.launches = 0

    async def __call__(self) -> tuple[Any, Any]:
        self.launches += 1
        return self.browser, self.browser


def robots_site(clock: FakeClock, robots: str) -> tuple[httpx.MockTransport, list[float]]:
    """robots.txt 만 돌려주는 스텁. 요청 시각을 기록한다."""
    times: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        times.append(clock.time())
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        raise AssertionError(f"렌더 경로가 httpx 로 페이지를 가져갔다: {request.url}")

    return httpx.MockTransport(handle), times


def build(
    robots: str = ROBOTS_ALLOW_ALL,
    *,
    html: str = RENDERED_HTML,
    status: int = 200,
    hang: bool = False,
    render_timeout: float = 60.0,
    challenged: tuple[str | None, ...] = (),
) -> tuple[Renderer, StubBrowser, StubLauncher, Fetcher]:
    clock = FakeClock()
    transport, _ = robots_site(clock, robots)
    fetcher = Fetcher(
        settings=Settings(
            crawl_user_agent=USER_AGENT,
            crawl_delay_seconds=DELAY_SECONDS,
            crawl_max_retries=0,
        ),
        transport=transport,
        clock=clock.time,
        sleep=clock.sleep,
    )
    browser = StubBrowser(clock, html=html, status=status, hang=hang, challenged=challenged)
    launcher = StubLauncher(browser)
    renderer = Renderer(
        fetcher,
        settings=Settings(render_timeout_seconds=render_timeout),
        launch=launcher,
    )
    return renderer, browser, launcher, fetcher


async def test_render_returns_the_rendered_html() -> None:
    renderer, browser, _, _ = build()

    result = await renderer.fetch(LIST_URL)

    assert result.text == RENDERED_HTML
    assert result.status_code == 200
    assert browser.urls() == [LIST_URL]
    await renderer.aclose()


async def test_robots_disallow_does_not_launch_a_browser() -> None:
    """robots 가 막으면 브라우저를 띄우지 않는다. 정적 경로와 같은 판정이어야 한다."""
    renderer, browser, launcher, _ = build(ROBOTS_DISALLOW_JOBS)

    with pytest.raises(RobotsDisallowedError):
        await renderer.fetch(LIST_URL)

    assert launcher.launches == 0
    assert browser.urls() == []


async def test_render_uses_the_configured_user_agent() -> None:
    """브라우저 기본 User-Agent 로 나가지 않는다. 위장은 하지 않는다."""
    renderer, browser, _, _ = build()

    await renderer.fetch(LIST_URL)

    assert browser.user_agents == [USER_AGENT]
    await renderer.aclose()


async def test_challenge_reopens_with_the_browser_user_agent() -> None:
    """우리 이름으로 챌린지를 받은 호스트만 브라우저 기본 User-Agent 로 다시 연다.

    같은 실행의 다음 페이지는 처음부터 기본값으로 연다 — 페이지마다 챌린지를 한 번씩 받지 않는다.
    """
    renderer, browser, _, _ = build(challenged=(USER_AGENT,))

    first = await renderer.fetch(LIST_URL)
    await renderer.fetch(LIST_URL + "/2")

    assert first.status_code == 200
    assert first.text == RENDERED_HTML
    assert browser.user_agents == [USER_AGENT, None, None]
    assert all(context.closed for context in browser.contexts)
    await renderer.aclose()


async def test_challenge_with_the_browser_user_agent_too_fails() -> None:
    """기본 User-Agent 로도 챌린지면 더 열지 않고 봇 확인 실패로 올린다."""
    renderer, browser, _, _ = build(challenged=(USER_AGENT, None))

    with pytest.raises(BotChallengeError):
        await renderer.fetch(LIST_URL)

    assert browser.user_agents == [USER_AGENT, None]
    await renderer.aclose()


async def test_same_host_renders_wait_the_delay() -> None:
    """같은 호스트로 이어지는 렌더 사이에 CRAWL_DELAY_SECONDS 가 실제로 들어간다."""
    renderer, browser, _, _ = build()

    await renderer.fetch(LIST_URL)
    await renderer.fetch(OTHER_URL)

    first, second = (moment for moment, _ in browser.visited)
    assert second - first >= DELAY_SECONDS
    await renderer.aclose()


async def test_browser_is_reused_across_pages() -> None:
    """실행 하나 안에서는 브라우저를 한 번만 띄운다."""
    renderer, _, launcher, _ = build()

    await renderer.fetch(LIST_URL)
    await renderer.fetch(OTHER_URL)

    assert launcher.launches == 1
    await renderer.aclose()


async def test_timeout_closes_the_browser() -> None:
    """렌더가 시간 제한을 넘기면 실패로 남고 브라우저는 닫힌다."""
    renderer, browser, _, _ = build(hang=True, render_timeout=0.05)

    with pytest.raises(RenderError) as caught:
        await renderer.fetch(LIST_URL)

    assert "시간 제한" in str(caught.value)
    assert caught.value.error_class == "transport"
    assert browser.closed is True
    assert browser.contexts[0].closed is True


async def test_close_shuts_the_browser_down() -> None:
    renderer, browser, _, _ = build()
    await renderer.fetch(LIST_URL)

    await renderer.aclose()

    assert browser.closed is True
    # 두 번 불러도 터지지 않는다. 종료 경로가 여러 개다
    await renderer.aclose()


async def test_open_source_static_does_not_build_a_renderer() -> None:
    """기본값은 정적이다. 렌더 경로는 명시적으로 올린 크롤러만 받는다."""
    renderer, _, launcher, fetcher = build()
    built: list[Renderer] = []

    def record(_: Any) -> Renderer:
        built.append(renderer)
        return renderer

    async with open_source(STATIC, fetcher, renderer=record) as source:
        assert source is fetcher

    assert built == []
    assert launcher.launches == 0


async def test_open_source_playwright_closes_the_renderer() -> None:
    """렌더 모드면 렌더러를 쓰고, 블록을 나갈 때 브라우저가 닫힌다."""
    renderer, browser, _, fetcher = build()

    async with open_source(PLAYWRIGHT, fetcher, renderer=lambda _: renderer) as source:
        assert source is renderer
        await source.fetch(LIST_URL)

    assert browser.closed is True


class EventPage:
    """요청 이벤트를 흘려보내는 페이지. `networkidle` 과 항목 힌트는 곧바로 통과한 척한다."""

    def __init__(self) -> None:
        self.handlers: dict[str, list[Any]] = {}
        self.html = "<ul><li>메뉴</li><li>메뉴</li><li>메뉴</li></ul>"

    def on(self, event: str, handler: Any) -> None:
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event: str, request: Any) -> None:
        for handler in self.handlers.get(event, []):
            handler(request)

    async def wait_for_load_state(self, state: str, **kwargs: Any) -> None:
        return None

    async def wait_for_function(self, *args: Any, **kwargs: Any) -> None:
        return None


def data_request(kind: str) -> Any:
    return type("Request", (), {"resource_type": kind})()


async def test_목록_API_가_늦어도_요청이_끝날_때까지_기다린다() -> None:
    """KT 실측(2026-10-21): 메뉴로 항목 힌트가 먼저 차, 늦게 온 목록을 빼고 읽었다."""
    page = EventPage()
    inflight = InFlight()
    inflight.attach(page)
    listing = data_request("fetch")
    page.emit("request", listing)
    page.emit("request", data_request("image"))

    async def answer() -> None:
        await asyncio.sleep(0.2)
        page.html = "<ul><li><a href='/jobs/1'>공고</a></li></ul>"
        page.emit("requestfinished", listing)

    task = asyncio.create_task(answer())
    await _settle(page, inflight)
    await task

    assert inflight.open == 0
    assert "/jobs/1" in page.html


async def test_실패한_요청도_끝난_것으로_센다() -> None:
    page = EventPage()
    inflight = InFlight()
    inflight.attach(page)
    request = data_request("xhr")
    page.emit("request", request)
    page.emit("requestfailed", request)

    assert await inflight.wait(1.0) is True
