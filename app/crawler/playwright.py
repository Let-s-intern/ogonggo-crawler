"""JS 로 그려지는 페이지를 브라우저로 렌더해 HTML 을 돌려준다.

정적 fetch 가 껍데기만 돌려주는 것이 측정으로 확인된 사이트에만 쓴다 (`seeds/sample-sites.json`).
기본 경로는 계속 정적이고, 렌더는 사이트별 승격이다 (`.claude/rules/crawling.md`).

## 정책은 여기서 다시 만들지 않는다

robots 확인과 호스트별 딜레이는 `app/crawler/fetcher.py` 의 `Fetcher.guard()` 가 그대로
적용한다. 이 모듈은 그 안에서 페이지를 그릴 뿐이다. 렌더 경로가 robots 를 건너뛰면
"모든 외부 요청은 공용 클라이언트를 지난다" 가 사실이 아니게 된다.

User-Agent 도 `Fetcher` 가 들고 있는 값(`CRAWL_USER_AGENT`)을 그대로 쓴다. 브라우저 기본
User-Agent 로 나가면 차단을 우회하는 것이 되고, 그것은 하지 않는다. 로그인·CAPTCHA 우회도
PRD 비목표다 — 여기서 하는 것은 렌더링뿐이다.

## 재시도하지 않는다

`Fetcher` 는 transport 실패를 백오프 재시도하지만 렌더 경로는 하지 않는다. 렌더 1회가 이미
정적 타임아웃의 몇 배를 쓰고, 실패한 렌더를 세 번 더 하면 한 실행이 실행 타임아웃을 넘겨
동시 실행 자리를 붙든다. 실패한 렌더는 다음 주기가 다시 시도한다.

## 브라우저 수명

한 실행 안에서는 브라우저 하나를 목록·상세에 재사용하고, 실행이 끝나면 닫는다. 브라우저
인스턴스 하나가 150~300MB 를 쓰기 때문에 실행 사이에 띄워 두지 않는다. `open_source()` 가
그 수명을 들고 있다.

## 페이지가 낸 요청을 관찰한다

`RequestLog` 는 렌더 중에 **페이지가 스스로 내는** 요청을 받아 적는다. 상세가 어느 endpoint
에서 오는지는 등록할 때 이것으로 알아낸다 — 삼성은 클릭해도 주소가 바뀌지 않고
`recruit/detail.data?seqno=...` 가 나갈 뿐이라, 요청을 보지 않으면 상세 경로를 찾을 길이 없다
(`.claude/site-recipes/www-samsungcareers-com.md`).

**이것은 관찰이지 두 번째 요청 경로가 아니다.** 여기서 새로 무엇을 보내지 않는다. 브라우저가
이미 낸 요청의 응답을 읽을 뿐이고, 그 브라우저는 `Fetcher.guard()` 안에서 돈다. 관찰로 알아낸
경로를 실제로 부르는 것은 공용 fetch 클라이언트다 (`.claude/rules/crawling.md`).

거르는 것과 상한이 있다. 정적 자산(`.js`, `.css`, 이미지, 폰트)과 분석 도구는 상세 경로가 될
수 없으므로 기록하지 않고, 응답 본문은 `OBSERVED_BODY_LIMIT` 까지만 들고 있는다. 페이지 하나가
수백 개의 요청을 내고 그중 하나가 수 MB 인 것이 보통이라, 상한이 없으면 관찰 자체가 메모리를
먹는다.

**요청 헤더도 함께 적는다.** 브라우저에서만 되던 요청이 `httpx` 로도 되는지가 헤더 하나로
갈린다 — HD현대 목록 API 는 `x-user-role` 이 없으면 500 이다. 담는 것은 사이트가 요구하는
기능성 헤더뿐이고, 브라우저가 스스로 붙인 것과 `cookie`·`authorization` 은 버린다. 그 둘은 그
브라우저 한 번의 신원이라 저장하면 오늘만 되는 크롤러가 남는다 (`.claude/rules/crawling.md`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from app.config import Settings, get_settings
from app.crawler.fetcher import (
    FetchError,
    FetchPolicy,
    FetchResult,
    PageSource,
    ResponseStatusError,
)

logger = logging.getLogger(__name__)

STATIC = "static"
PLAYWRIGHT = "playwright"
RENDER_MODES: tuple[str, ...] = (STATIC, PLAYWRIGHT)

# 로드가 끝난 뒤 XHR 이 잦아들기를 기다리는 시간. 이 시간이 지나도 조용해지지 않으면 그 시점의
# DOM 을 그대로 쓴다 — 광고나 폴링 때문에 영영 조용해지지 않는 페이지가 있다.
_SETTLE_SECONDS = 5.0
# 목록을 XHR 로 채우는 사이트는 networkidle 이 잦아든 뒤에도 DOM 이 비어 있을 수 있다.
# 현대자동차가 그랬다 — 같은 셀렉터가 어떤 실행에는 20건, 어떤 실행에는 0건이었다.
# 반복 항목이 실제로 생길 때까지 한 번 더 기다린다.
_ITEMS_SECONDS = 10.0
_ITEM_HINTS = ("li[data-recucls]", "ul li", "ol li", "tbody tr", "article")

# 응답 본문을 여기까지만 들고 있는다. 상세 경로를 알아보는 데는 앞부분이면 충분하고, 큰 응답을
# 통째로 쥐고 있으면 관찰이 렌더보다 비싸진다
OBSERVED_BODY_LIMIT = 200_000
# 한 페이지에서 기록할 요청 수의 상한. 폴링하는 페이지가 무한히 쌓는 것을 막는다
OBSERVED_LIMIT = 200

# 브라우저가 스스로 붙이는 헤더. 설정에 담을 것은 사이트가 요구하는 기능성 헤더뿐이라 이
# 이름들은 버린다. `cookie` 와 `authorization` 은 그 브라우저 한 번의 신원이고, `user-agent`
# 는 공용 fetch 클라이언트가 정한다 (`app/selector/api_schema.py` 의 `BLOCKED_HEADERS`)
_BROWSER_HEADERS: frozenset[str] = frozenset(
    {
        "accept",
        "accept-encoding",
        "accept-language",
        "authorization",
        "cache-control",
        "connection",
        "content-length",
        "content-type",
        "cookie",
        "dnt",
        "host",
        "origin",
        "pragma",
        "priority",
        "range",
        # `referer` 는 설정에 넣을지를 판정이 따로 정한다 (`app/selector/list_api.py`)
        "referer",
        "te",
        "upgrade-insecure-requests",
        "user-agent",
        # 오류 추적 도구가 요청마다 새로 붙이는 값. 동원 목록 API 요청에 `sentry-trace` 가 있었다
        "baggage",
        "sentry-trace",
        "traceparent",
        "tracestate",
    }
)
# 이 접두사로 시작하는 것도 브라우저가 붙인 것이다
_BROWSER_HEADER_PREFIXES: tuple[str, ...] = ("sec-", "if-", "proxy-")
# 요청 하나에서 적어 둘 헤더 수와 값 길이의 상한. 긴 값은 토큰이지 기능성 헤더가 아니다
_HEADER_LIMIT = 8
_HEADER_VALUE_LIMIT = 200

# 상세 경로가 될 수 없는 파일들. 확장자로 먼저 거른다
_ASSET_SUFFIXES: tuple[str, ...] = (
    ".js",
    ".mjs",
    ".css",
    ".map",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".mp4",
    ".webm",
)
# 분석·광고 도구. 공고와 무관한 요청이고 페이지마다 수십 개씩 나간다
_ANALYTICS_HOST_MARKS: tuple[str, ...] = (
    "google",
    "gtm",
    "analytics",
    "doubleclick",
    "facebook",
    "hotjar",
    "clarity.ms",
    "criteo",
    "adservice",
    "amplitude",
    "mixpanel",
)
_ANALYTICS_PATH_MARKS: tuple[str, ...] = ("/gtm", "/gtag", "/analytics", "/ga.js")
# 확장자가 없는 자산을 content-type 으로 한 번 더 거른다
_ASSET_TYPE_MARKS: tuple[str, ...] = (
    "image/",
    "font/",
    "video/",
    "audio/",
    "text/css",
    "javascript",
)


@dataclass(frozen=True)
class ObservedRequest:
    """페이지가 스스로 낸 요청 하나와 그 응답.

    `body` 는 `OBSERVED_BODY_LIMIT` 까지 자른 것이고, 잘렸으면 `truncated` 가 참이다.

    `request_headers` 는 사이트가 요구하는 기능성 헤더만 남긴 것이다. 이 요청을 `httpx` 로
    다시 부르거나 설정에 담을 때 그대로 쓴다 (`functional_headers`).
    """

    method: str
    url: str
    status: int
    content_type: str = ""
    request_body: str = ""
    body: str = ""
    truncated: bool = False
    # 기본값이 있어 이 필드가 생기기 전에 만들어진 호출과 시험이 그대로 통과한다
    request_headers: Mapping[str, str] = field(default_factory=dict)

    @property
    def is_json(self) -> bool:
        """JSON 응답인가. content-type 이 비어 오는 API 가 있어 본문 첫 글자도 본다."""
        if "json" in self.content_type:
            return True
        return self.body.lstrip()[:1] in ("{", "[")

    def contains(self, text: str) -> bool:
        """이 요청 어딘가에 그 값이 들어 있는가. 공고 번호를 찾을 때 쓴다."""
        if not text:
            return False
        return text in self.url or text in self.request_body


class RequestLog:
    """렌더 중 페이지가 낸 요청을 받아 적는다. 새로 보내는 것은 없다.

    `attach()` 로 페이지의 응답 이벤트에 붙고, 응답 본문은 백그라운드로 읽는다. 읽기가 끝나기를
    기다리는 것은 `drain()` 이고, **페이지가 닫히기 전에** 불러야 한다 — 닫힌 뒤에는 본문을
    읽을 수 없다.

    기록 순서는 응답이 온 순서다. 본문을 읽는 시간이 제각각이라 완료 순서로 담으면 클릭 전후가
    뒤섞이는데, 무엇이 클릭 때문에 나간 요청인지가 이 순서로 갈린다. 그래서 자리를 먼저 잡아
    두고 나중에 채운다.
    """

    def __init__(
        self, *, body_limit: int = OBSERVED_BODY_LIMIT, limit: int = OBSERVED_LIMIT
    ) -> None:
        self._body_limit = body_limit
        self._limit = limit
        self._slots: list[ObservedRequest | None] = []
        self._pending: list[asyncio.Future[None]] = []
        # 페이지 이동 요청의 주소. 리다이렉트 전 주소까지 남는다 — `window.open` 으로 연 새 탭은
        # 응답 기록이 붙기 전에 첫 요청이 지나가고, 도착한 주소는 리다이렉트 뒤라 공고 번호가
        # 빠져 있다(GC녹십자, 2026-09-22)
        self._navigations: list[str] = []

    def attach(self, page: Any) -> None:
        """페이지의 응답 이벤트에 붙는다. 같은 로그를 여러 페이지에 붙일 수 있다 —
        새 탭에서 나가는 요청도 같은 자리에 모인다."""
        page.on("response", self._on_response)

    def watch_navigations(self, context: Any) -> None:
        """컨텍스트의 페이지 이동 요청을 적는다. 새 탭의 첫 요청도 여기서는 놓치지 않는다."""
        context.on("request", self._on_request)

    def navigation_mark(self) -> int:
        return len(self._navigations)

    def navigations_since(self, mark: int) -> list[str]:
        """표시한 자리 뒤에 일어난 페이지 이동의 주소. 같은 주소는 한 번만 담는다."""
        return list(dict.fromkeys(self._navigations[mark:]))

    def _on_request(self, request: Any) -> None:
        with suppress(Exception):
            if not request.is_navigation_request():
                return
            if not _top_level(request):
                return
            url = str(request.url or "")
            if is_data_request(url):
                self._navigations.append(url)

    @property
    def requests(self) -> list[ObservedRequest]:
        """지금까지 기록된 요청. 아직 안 읽혔거나 걸러진 자리는 빠진다."""
        return [entry for entry in self._slots if entry is not None]

    def mark(self) -> int:
        """지금 자리를 표시한다. 클릭 전에 찍어 두고 `since()` 로 그 뒤만 본다."""
        return len(self._slots)

    def since(self, mark: int) -> list[ObservedRequest]:
        """표시한 자리 뒤에 기록된 요청."""
        return [entry for entry in self._slots[mark:] if entry is not None]

    async def drain(self) -> None:
        """본문 읽기가 끝나기를 기다린다. 페이지를 닫기 전에 부른다."""
        while self._pending:
            pending, self._pending = self._pending, []
            await asyncio.gather(*pending, return_exceptions=True)

    def _on_response(self, response: Any) -> None:
        """이벤트 콜백. 여기서는 자리만 잡고 본문 읽기는 뒤로 넘긴다."""
        url = str(getattr(response, "url", "") or "")
        if not is_data_request(url) or len(self._slots) >= self._limit:
            return
        slot = len(self._slots)
        self._slots.append(None)
        self._pending.append(asyncio.ensure_future(self._capture(slot, response)))

    async def _capture(self, slot: int, response: Any) -> None:
        """응답 하나를 기록한다. 못 읽으면 그 자리는 빈 채로 둔다."""
        content_type = _content_type(response)
        if any(mark in content_type for mark in _ASSET_TYPE_MARKS):
            return

        body = ""
        with suppress(Exception):
            body = await response.text()
        request = getattr(response, "request", None)
        self._slots[slot] = ObservedRequest(
            method=str(getattr(request, "method", "") or "GET").upper(),
            url=str(getattr(response, "url", "") or ""),
            status=int(getattr(response, "status", 0) or 0),
            content_type=content_type,
            request_body=str(getattr(request, "post_data", "") or ""),
            body=body[: self._body_limit],
            truncated=len(body) > self._body_limit,
            request_headers=functional_headers(getattr(request, "headers", None)),
        )


def _top_level(request: Any) -> bool:
    """최상위 문서로 가는 이동인가. iframe 안의 이동은 상세가 아니다.

    새 탭의 첫 이동은 탭이 붙기 전이라 frame 을 읽으면 예외가 난다. 그것은 새 탭의 최상위
    이동이라 참으로 본다.
    """
    try:
        return request.frame.parent_frame is None
    except Exception:
        return True


def functional_headers(headers: Any) -> dict[str, str]:
    """요청 헤더 중 사이트가 요구하는 것만. 브라우저가 스스로 붙인 것은 버린다.

    남기는 기준이 목록이 아니라 제외 목록인 이유는, 사이트가 무엇을 요구할지 미리 알 수 없기
    때문이다. HD현대는 `x-user-role`, 현대자동차는 `x-hkmc-service` 다 — 이름을 모아 두는
    방식으로는 다음 사이트에서 또 막힌다.

    `cookie` 와 `authorization` 은 여기서 떨어진다. 그 브라우저 한 번의 신원이라 설정에 저장하면
    등록한 날만 되는 크롤러가 남고, 그 실패는 "어제는 됐는데" 로 보여 원인을 찾기 어렵다.
    """
    if not isinstance(headers, Mapping):
        return {}
    found: dict[str, str] = {}
    for name, value in headers.items():
        key = str(name).strip().lower()
        text = str(value).strip()
        if not key or not text or len(text) > _HEADER_VALUE_LIMIT:
            continue
        if key in _BROWSER_HEADERS or key.startswith(_BROWSER_HEADER_PREFIXES):
            continue
        found[key] = text
        if len(found) >= _HEADER_LIMIT:
            break
    return found


def is_data_request(url: str) -> bool:
    """이 URL 을 기록할 것인가. 자산과 분석 도구는 상세 경로가 될 수 없다."""
    value = url.strip()
    if not value.lower().startswith(("http://", "https://")):
        # data:, blob:, about:blank. 다시 부를 수 있는 주소가 아니다
        return False
    parts = urlsplit(value.lower())
    if any(mark in parts.netloc for mark in _ANALYTICS_HOST_MARKS):
        return False
    if any(mark in parts.path for mark in _ANALYTICS_PATH_MARKS):
        return False
    return not parts.path.endswith(_ASSET_SUFFIXES)


def _content_type(response: Any) -> str:
    """응답의 content-type. 버전에 따라 속성이거나 메서드라 둘 다 받는다."""
    headers = getattr(response, "headers", None)
    if callable(headers):
        with suppress(Exception):
            headers = headers()
    if not isinstance(headers, dict):
        return ""
    for name, value in headers.items():
        if str(name).lower() == "content-type":
            return str(value).lower()
    return ""


@dataclass(frozen=True)
class ProbeSession:
    """렌더해 놓고 아직 닫지 않은 페이지 하나. 등록할 때 눌러 보는 자리가 쓴다.

    `html` 과 `url` 은 클릭 전의 것이다. 클릭 뒤의 값은 누른 쪽이 다시 읽는다.
    """

    page: Any
    context: Any
    log: RequestLog
    html: str
    url: str


class RenderError(FetchError):
    """렌더 실패. 타임아웃과 브라우저 오류가 여기 들어온다."""

    error_class = "transport"


class RenderUnavailableError(FetchError):
    """브라우저가 설치돼 있지 않다.

    transport 실패가 아니다. 사이트는 아무 잘못이 없고 재시도해도 같은 결과다. 배포에
    Chromium 이 빠진 것이므로 `error_class` 를 비우고 사유만 남긴다.
    """

    error_class = None


class Renderer:
    """렌더 경로의 `PageSource`. `Fetcher` 와 같은 자리에 꽂힌다.

    `launch` 는 테스트가 브라우저 없이 돌리기 위해 주입 가능하다. 운영에서는 Playwright 로
    Chromium 을 띄운다.
    """

    def __init__(
        self,
        fetcher: FetchPolicy,
        *,
        settings: Settings | None = None,
        launch: Callable[[], Any] | None = None,
    ) -> None:
        resolved = settings or get_settings()
        self._fetcher = fetcher
        self._timeout = resolved.render_timeout_seconds
        self._launch = launch or _launch_chromium
        self._playwright: Any | None = None
        self._browser: Any | None = None

    async def fetch(self, url: str) -> FetchResult:
        """robots 와 딜레이를 통과한 URL 만 렌더한다.

        `Fetcher.fetch()` 와 같은 값을 돌려준다. 러너와 파서는 어느 경로로 왔는지 모른다.
        """
        async with self._fetcher.guard(url):
            return await self._render(url)

    async def fetch_observed(self, url: str) -> tuple[FetchResult, list[ObservedRequest]]:
        """렌더하면서 페이지가 낸 요청까지 함께 돌려준다.

        등록할 때 상세 경로를 찾는 자리만 쓴다. 주기 실행은 `fetch()` 를 쓴다 — 매 실행마다
        응답 본문을 다 들고 있을 이유가 없다.
        """
        log = RequestLog()
        async with self._fetcher.guard(url):
            result = await self._render(url, log=log)
        return result, log.requests

    @asynccontextmanager
    async def open_probe(self, url: str) -> AsyncIterator[ProbeSession]:
        """목록을 렌더한 채로 열어 둔다. 등록할 때 항목을 눌러 보는 자리만 쓴다.

        `fetch()` 는 HTML 한 장을 받고 페이지를 닫는다. 눌러 보려면 페이지가 살아 있어야 하고,
        누른 뒤에 나가는 요청까지 같은 로그에 모여야 한다. 새 탭도 같은 로그에 붙인다 — SK 는
        상세가 새 탭에서 열린다.

        호스트 잠금은 이 블록이 끝날 때까지 잡고 있다. 브라우저가 페이지 하나를 그리며 같은
        호스트로 여러 요청을 내는 동안 정적 요청이 끼어들면 딜레이가 사실이 아니게 된다
        (`app/crawler/fetcher.py` 의 `guard()`).
        """
        browser = await self._browser_instance()
        timeout_ms = int(self._timeout * 1000)
        async with self._fetcher.guard(url):
            context = await browser.new_context(user_agent=self._fetcher.user_agent)
            log = RequestLog()
            try:
                page = await context.new_page()
                log.attach(page)
                context.on("page", log.attach)
                log.watch_navigations(context)
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                await _settle(page)
                await log.drain()
                yield ProbeSession(
                    page=page, context=context, log=log, html=await page.content(), url=page.url
                )
            finally:
                with suppress(Exception):
                    await log.drain()
                with suppress(Exception):
                    await context.close()

    async def aclose(self) -> None:
        """브라우저를 닫는다. 두 번 불러도 안전하다."""
        browser, self._browser = self._browser, None
        playwright, self._playwright = self._playwright, None
        if browser is not None:
            with suppress(Exception):
                await browser.close()
        if playwright is not None:
            with suppress(Exception):
                await playwright.stop()

    async def _render(self, url: str, *, log: RequestLog | None = None) -> FetchResult:
        browser = await self._browser_instance()
        timeout_ms = int(self._timeout * 1000)
        try:
            async with asyncio.timeout(self._timeout):
                context = await browser.new_context(user_agent=self._fetcher.user_agent)
                try:
                    page = await context.new_page()
                    if log is not None:
                        log.attach(page)
                    response = await page.goto(
                        url, wait_until="domcontentloaded", timeout=timeout_ms
                    )
                    await _settle(page)
                    html = await page.content()
                    final_url = page.url
                    if log is not None:
                        # 페이지가 닫히면 응답 본문을 읽을 수 없다. 닫기 전에 기다린다
                        await log.drain()
                finally:
                    with suppress(Exception):
                        await context.close()
        except TimeoutError as exc:
            # 어디까지 갔는지 알 수 없는 브라우저를 다음 페이지에 재사용하지 않는다.
            await self.aclose()
            raise RenderError(f"렌더가 시간 제한 {self._timeout}초를 넘겼다: {url}") from exc
        except FetchError:
            raise
        except Exception as exc:
            await self.aclose()
            raise RenderError(f"렌더 실패({type(exc).__name__}): {url}") from exc

        status = _status(response)
        if status >= 400:
            # 정적 경로와 같은 판정이다. 4xx·5xx 는 렌더된 껍데기가 아니라 실패다
            raise ResponseStatusError(f"응답 {status}: {url}", status)

        logger.info("렌더 완료 url=%s status=%s chars=%d", url, status, len(html))
        return FetchResult(url=final_url or url, status_code=status, text=html)

    async def _browser_instance(self) -> Any:
        """한 실행 안에서 브라우저 하나를 재사용한다. 페이지마다 새로 띄우지 않는다."""
        if self._browser is None:
            self._playwright, self._browser = await self._launch()
        return self._browser


async def _settle(page: Any) -> None:
    """XHR 이 잦아들고 반복 항목이 생길 때까지 기다린다. 안 되어도 실패로 보지 않는다.

    `networkidle` 만 보면 목록을 늦게 채우는 사이트에서 빈 DOM 을 가져온다. 그 결과는
    `selector_miss` 로 남는데, 셀렉터는 멀쩡하고 기다림이 짧았을 뿐이라 운영자가 엉뚱한
    곳을 고치게 된다.

    여기서 못 기다려도 실패로 만들지 않는다. 항목이 정말 없는 사이트도 있고, 그 판정은
    셀렉터를 아는 파서의 몫이다.
    """
    with suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=int(_SETTLE_SECONDS * 1000))
    with suppress(Exception):
        await page.wait_for_function(
            "(hints) => hints.some((h) => document.querySelectorAll(h).length >= 3)",
            arg=list(_ITEM_HINTS),
            timeout=int(_ITEMS_SECONDS * 1000),
        )


def _status(response: Any) -> int:
    """네비게이션 응답의 상태 코드. 응답 자체가 없으면 판정하지 않고 0 으로 둔다."""
    if response is None:
        return 0
    value = getattr(response, "status", None)
    return int(value) if value is not None else 0


async def _launch_chromium() -> tuple[Any, Any]:
    """운영 경로. 임포트를 함수 안에서 하는 이유는 정적 실행만 하는 배포에서 Playwright 가
    없어도 앱이 뜨게 하기 위해서다."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RenderUnavailableError(
            "playwright 패키지가 없다. 렌더 모드는 Chromium 이 설치된 이미지에서만 된다"
        ) from exc

    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.launch(headless=True)
    except Exception as exc:
        await playwright.stop()
        raise RenderUnavailableError(f"Chromium 을 띄우지 못했다: {exc}") from exc
    return playwright, browser


@asynccontextmanager
async def open_source(
    render_mode: str | None,
    fetcher: FetchPolicy,
    *,
    renderer: Callable[[FetchPolicy], Renderer] | None = None,
) -> AsyncIterator[PageSource]:
    """이 실행이 쓸 `PageSource` 를 고른다. 브라우저 수명이 이 블록이다.

    `render_mode` 가 `playwright` 인 경우에만 브라우저를 띄운다. 값이 없거나 모르는 값이면
    정적이다 — 렌더는 운영자가 명시적으로 올린 사이트만 받는다.
    """
    if render_mode != PLAYWRIGHT:
        yield fetcher
        return

    build = renderer or (lambda client: Renderer(client))
    instance = build(fetcher)
    try:
        yield instance
    finally:
        await instance.aclose()
