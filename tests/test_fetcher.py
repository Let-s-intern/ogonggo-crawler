"""공용 fetch 클라이언트 테스트.

네트워크에 나가지 않는다. 응답은 전부 `httpx.MockTransport` 스텁이고, 시간은 sleep 이 불릴 때만
흐르는 가짜 시계라 실제로 기다리는 구간이 없다.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from app.config import Settings
from app.crawler.fetcher import (
    Fetcher,
    ResponseStatusError,
    RobotsDisallowedError,
    TransportError,
)

USER_AGENT = "job-crawler-automation (contact: test@example.test)"
DELAY_SECONDS = 3.0
MAX_RETRIES = 3

ROBOTS_ALLOW_ALL = "User-agent: *\nDisallow:\n"
ROBOTS_DISALLOW_JOBS = "User-agent: *\nDisallow: /jobs\n"

LIST_URL = "https://example.test/jobs/1"
OTHER_URL = "https://example.test/jobs/2"
ROBOTS_URL = "https://example.test/robots.txt"


class FakeClock:
    """sleep 이 호출될 때만 흐르는 시계."""

    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class StubSite:
    """요청을 시각과 함께 기록하는 스텁 사이트."""

    def __init__(
        self, clock: FakeClock, responder: Callable[[httpx.Request], httpx.Response]
    ) -> None:
        self._clock = clock
        self._responder = responder
        self.requests: list[tuple[float, str]] = []
        self.headers: list[httpx.Headers] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((self._clock.time(), str(request.url)))
        self.headers.append(request.headers)
        return self._responder(request)

    def urls(self, *, exclude_robots: bool = False) -> list[str]:
        return [
            url for _, url in self.requests if not (exclude_robots and url.endswith("/robots.txt"))
        ]

    def times(self) -> list[float]:
        return [moment for moment, _ in self.requests]


def robots_then(
    robots: str, page: Callable[[httpx.Request], httpx.Response]
) -> Callable[[httpx.Request], httpx.Response]:
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots)
        return page(request)

    return responder


def ok_page(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text=f"<html>{request.url.path}</html>")


def make_fetcher(
    site: StubSite,
    clock: FakeClock,
    *,
    delay: float = DELAY_SECONDS,
    max_retries: int = MAX_RETRIES,
) -> Fetcher:
    settings = Settings(
        crawl_user_agent=USER_AGENT,
        crawl_delay_seconds=delay,
        crawl_timeout_seconds=5.0,
        crawl_max_retries=max_retries,
    )
    return Fetcher(
        settings=settings,
        transport=site.transport,
        clock=clock.time,
        sleep=clock.sleep,
    )


async def test_same_host_requests_keep_the_delay() -> None:
    """같은 호스트로 연속 요청할 때 요청 사이 간격이 CRAWL_DELAY_SECONDS 이상이다."""
    clock = FakeClock()
    site = StubSite(clock, robots_then(ROBOTS_ALLOW_ALL, ok_page))
    fetcher = make_fetcher(site, clock)

    await fetcher.fetch(LIST_URL)
    await fetcher.fetch(OTHER_URL)
    await fetcher.aclose()

    # robots.txt 도 같은 호스트로 가는 요청이므로 딜레이 계산에 들어간다.
    assert site.urls() == [ROBOTS_URL, LIST_URL, OTHER_URL]
    times = site.times()
    assert times[1] - times[0] >= DELAY_SECONDS
    assert times[2] - times[1] >= DELAY_SECONDS


async def test_delay_is_per_host() -> None:
    """다른 호스트는 서로의 딜레이를 기다리지 않는다."""
    clock = FakeClock()
    site = StubSite(clock, robots_then(ROBOTS_ALLOW_ALL, ok_page))
    fetcher = make_fetcher(site, clock)

    await fetcher.fetch("https://a.test/jobs")
    await fetcher.fetch("https://b.test/jobs")
    await fetcher.aclose()

    # b.test 의 첫 요청은 a.test 의 마지막 요청과 같은 시각에 나간다 — 서로 기다리지 않는다.
    assert site.requests == [
        (0.0, "https://a.test/robots.txt"),
        (DELAY_SECONDS, "https://a.test/jobs"),
        (DELAY_SECONDS, "https://b.test/robots.txt"),
        (DELAY_SECONDS * 2, "https://b.test/jobs"),
    ]


async def test_server_error_is_retried_up_to_max_retries() -> None:
    """5xx 는 최초 1회 + CRAWL_MAX_RETRIES 회까지 요청된다."""
    clock = FakeClock()
    site = StubSite(clock, robots_then(ROBOTS_ALLOW_ALL, lambda _: httpx.Response(503)))
    fetcher = make_fetcher(site, clock, max_retries=MAX_RETRIES)

    with pytest.raises(TransportError):
        await fetcher.fetch(LIST_URL)
    await fetcher.aclose()

    assert site.urls(exclude_robots=True) == [LIST_URL] * (1 + MAX_RETRIES)


async def test_connection_failure_is_retried() -> None:
    """연결 끊김도 transport 실패다."""

    def page(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection reset", request=request)

    clock = FakeClock()
    site = StubSite(clock, robots_then(ROBOTS_ALLOW_ALL, page))
    fetcher = make_fetcher(site, clock, max_retries=2)

    with pytest.raises(TransportError):
        await fetcher.fetch(LIST_URL)
    await fetcher.aclose()

    assert site.urls(exclude_robots=True) == [LIST_URL] * 3


async def test_client_error_is_not_retried() -> None:
    """4xx 는 다시 물어도 같은 답이므로 재시도하지 않는다."""
    clock = FakeClock()
    site = StubSite(clock, robots_then(ROBOTS_ALLOW_ALL, lambda _: httpx.Response(404)))
    fetcher = make_fetcher(site, clock)

    with pytest.raises(ResponseStatusError) as caught:
        await fetcher.fetch(LIST_URL)
    await fetcher.aclose()

    assert caught.value.status_code == 404
    assert site.urls(exclude_robots=True) == [LIST_URL]


async def test_robots_disallow_blocks_the_request() -> None:
    """disallow 면 대상 URL 로 요청이 한 번도 나가지 않고 실패한다."""
    clock = FakeClock()
    site = StubSite(clock, robots_then(ROBOTS_DISALLOW_JOBS, ok_page))
    fetcher = make_fetcher(site, clock)

    with pytest.raises(RobotsDisallowedError):
        await fetcher.fetch(LIST_URL)
    await fetcher.aclose()

    assert site.urls() == [ROBOTS_URL]


async def test_robots_verdict_is_cached_per_host() -> None:
    """robots.txt 는 호스트당 한 번만 가져온다."""
    clock = FakeClock()
    site = StubSite(clock, robots_then(ROBOTS_ALLOW_ALL, ok_page))
    fetcher = make_fetcher(site, clock)

    await fetcher.fetch(LIST_URL)
    await fetcher.fetch(OTHER_URL)
    await fetcher.aclose()

    assert site.urls().count(ROBOTS_URL) == 1


async def test_missing_robots_allows_the_request() -> None:
    """robots.txt 가 404 면 규칙이 없는 것으로 본다."""
    clock = FakeClock()

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return ok_page(request)

    site = StubSite(clock, responder)
    fetcher = make_fetcher(site, clock)

    result = await fetcher.fetch(LIST_URL)
    await fetcher.aclose()

    assert result.status_code == 200
    assert site.urls(exclude_robots=True) == [LIST_URL]


async def test_unreachable_robots_does_not_fetch_the_target() -> None:
    """robots 판정을 얻지 못하면 대상 요청을 보내지 않는다."""
    clock = FakeClock()

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(500)
        return ok_page(request)

    site = StubSite(clock, responder)
    fetcher = make_fetcher(site, clock, max_retries=1)

    with pytest.raises(TransportError):
        await fetcher.fetch(LIST_URL)
    await fetcher.aclose()

    assert site.urls(exclude_robots=True) == []


async def test_user_agent_is_the_configured_one() -> None:
    """설정값을 그대로 보낸다. 브라우저 위장을 하지 않는다."""
    clock = FakeClock()
    site = StubSite(clock, robots_then(ROBOTS_ALLOW_ALL, ok_page))
    fetcher = make_fetcher(site, clock)

    await fetcher.fetch(LIST_URL)
    await fetcher.aclose()

    assert all(headers["user-agent"] == USER_AGENT for headers in site.headers)


def _euc_kr_page(*, header_charset: bool) -> Callable[[httpx.Request], httpx.Response]:
    html = (
        '<html><head><meta http-equiv="Content-Type" content="text/html; charset=euc-kr" />'
        "</head><body><a>2026년 하반기 신규채용 공고</a></body></html>"
    )
    content_type = "text/html; charset=euc-kr" if header_charset else "text/html"

    def page(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=html.encode("cp949"), headers={"content-type": content_type}
        )

    return page


@pytest.mark.parametrize("header_charset", [False, True])
async def test_헤더에_문자셋이_없으면_meta_선언대로_읽는다(header_charset: bool) -> None:
    """카페스 실측(2026-09-22): 헤더는 `text/html` 뿐이고 `<meta>` 에만 euc-kr 이 있었다."""
    clock = FakeClock()
    site = StubSite(clock, robots_then("", _euc_kr_page(header_charset=header_charset)))
    fetcher = make_fetcher(site, clock, delay=0.0)
    try:
        result = await fetcher.fetch("http://kapes.test/employment/advertise.asp")
    finally:
        await fetcher.aclose()

    assert "2026년 하반기 신규채용 공고" in result.text
