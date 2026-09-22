"""등록할 때 목록 항목을 한 번 눌러 보고, 상세에 도달했는지 판정한다.

운영자가 상세 주소를 손으로 찾아 넣지 않게 하기 위한 자리다. 항목 속성만으로 상세 주소를 만들
수 있으면 여기까지 오지 않는다 (`app/selector/link.py`). 그것으로도 안 될 때 마지막으로
눌러 본다.

**클릭은 등록할 때 한 번이다.** 주기 실행은 여기를 지나지 않는다 — 알아낸 경로를 공용 fetch
클라이언트로 부를 뿐이다 (`.claude/rules/crawling.md`).

## 성공 판정이 "주소가 바뀌었나" 가 아니다

2026-08-25 측정에서 여섯 사이트의 클릭 결과가 넷으로 갈렸다.

| 신호 | 사이트 | 무슨 일이 있었나 |
|---|---|---|
| 주소가 바뀐다 | 롯데, LG, 한화 | 같은 탭에서 상세로 이동한다 |
| 새 탭이 열린다 | SK | 목록 탭은 그대로 있다 |
| 요청이 나간다 | 삼성 | 주소는 그대로고 `detail.data` 가 나간다 |
| 본문이 길어진다 | 삼성 | 모달이 열려 1,620자에서 9,798자가 됐다 |

주소만 보면 삼성이 실패로 읽힌다. **넷 중 하나라도 바뀌면 도달**이고, 넷 다 없을 때만
`detail_unreachable` 이다 (`app/crawler/failures.py`).

## 누르면 안 되는 것

현대는 항목의 첫 `a` 들이 SNS 공유 버튼이다 — `href="javascript:;"`,
`onclick="shareSns('facebook', ...)"`. 눌러도 아무 데도 가지 않고, 그것을 실패로 읽으면
사이트가 등록 불가로 판정된다. 실제로는 항목의 `data-recuyy`·`data-recutype`·`data-recucls`
로 상세 주소를 만들 수 있다 (`.claude/site-recipes/talent-hyundai-com.md`).

## 목록이 채워진 뒤에 누른다

삼성은 목록을 늦게 채운다. 2026-08-24 측정은 항목 0건인 채로 눌러 "이동하지 않음" 으로
판정했고, 다음 날 항목이 잡힌 뒤에 누르니 모달이 열렸다. 항목이 없는 것은 클릭 실패가 아니라
`list_empty` 다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Protocol

from app.crawler.failures import LIST_EMPTY
from app.crawler.playwright import ObservedRequest, RequestLog

logger = logging.getLogger(__name__)

# 도달 신호 넷. 사람이 읽을 근거 문장에 그대로 들어간다
SIGNAL_URL = "주소가 바뀌었다"
SIGNAL_TAB = "새 탭이 열렸다"
SIGNAL_REQUEST = "요청이 나갔다"
SIGNAL_BODY = "본문이 길어졌다"

# 넷 다 없을 때의 사유. `crawl_run_failures.reason` 과 같은 값이다
DETAIL_UNREACHABLE = "detail_unreachable"

# 클릭 뒤 이 시간만큼 기다렸다가 무엇이 바뀌었는지 본다. 측정에서 같은 탭 이동이 1.27~1.84초
# 였으므로 그보다 넉넉하게 둔다
CLICK_SETTLE_SECONDS = 3.0

# 목록이 채워지기를 기다리는 시간과 확인 간격. 삼성이 늦게 채운다
ITEMS_WAIT_SECONDS = 10.0
ITEMS_POLL_SECONDS = 0.5

# 본문이 이만큼 늘어야 신호로 본다. 시계나 배너가 몇 글자 바꾸는 것은 상세 도달이 아니다.
# 삼성 모달은 8,178자가 늘었다
BODY_GROWTH_CHARS = 500

# 한 항목에서 눌러 볼 `a` 의 최대 수. 항목 하나에 링크가 여남은 개인 사이트가 있고, 전부
# 눌러 보면 등록 한 번에 클릭이 수십 번 나간다
MAX_ANCHORS = 3


class ElementHandle(Protocol):
    """페이지 안의 요소 하나. Playwright 의 `ElementHandle` 과 같은 모양이다."""

    async def click(self, **kwargs: Any) -> None: ...

    async def get_attribute(self, name: str) -> str | None: ...

    async def query_selector_all(self, selector: str) -> list[Any]: ...


class ProbePage(Protocol):
    """클릭해 볼 페이지. 이미 목록이 열려 있는 페이지다."""

    url: str

    async def content(self) -> str: ...

    async def query_selector_all(self, selector: str) -> list[Any]: ...


class ProbeContext(Protocol):
    """탭 목록을 들고 있는 쪽. 새 탭이 열렸는지는 이것으로 안다."""

    @property
    def pages(self) -> list[Any]: ...


@dataclass(frozen=True)
class ClickOutcome:
    """클릭 한 번의 결과.

    `reached` 가 참이면 `url` 과 `html` 이 도달한 곳의 것이고, `requests` 는 그때 나간
    요청이다. 거짓이면 `failure` 가 `list_empty` 이거나 `detail_unreachable` 이다.
    """

    reached: bool
    signals: tuple[str, ...] = ()
    url: str = ""
    html: str = ""
    requests: tuple[ObservedRequest, ...] = ()
    # 누른 뒤 일어난 페이지 이동의 주소. 리다이렉트 전 주소까지 담긴다 (`RequestLog`)
    navigations: tuple[str, ...] = ()
    target: str = ""
    failure: str = ""
    reason: str = ""
    item_count: int = 0
    skipped: tuple[str, ...] = ()


async def probe_click(
    page: ProbePage,
    *,
    context: ProbeContext,
    log: RequestLog,
    item_selector: str,
    settle_seconds: float = CLICK_SETTLE_SECONDS,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> ClickOutcome:
    """항목 하나를 눌러 보고 무엇이 바뀌었는지 판정한다.

    항목 컨테이너를 먼저 누르고, 아무 일도 없으면 항목 안의 `a` 를 차례로 누른다. SNS 공유
    버튼은 건너뛴다.
    """
    wait = sleep or asyncio.sleep
    items = await wait_for_items(page, item_selector, sleep=wait)
    if not items:
        return ClickOutcome(
            reached=False,
            failure=LIST_EMPTY,
            reason=(
                f"`{item_selector}` 이 {ITEMS_WAIT_SECONDS:.0f}초를 기다려도 항목을 잡지 못했다. "
                "클릭이 실패한 것이 아니라 누를 항목이 없다"
            ),
        )

    item = items[0]
    targets, skipped = await _targets(item)
    for name, target in targets:
        outcome = await _click_and_judge(
            page,
            context=context,
            log=log,
            target=target,
            name=name,
            settle_seconds=settle_seconds,
            sleep=wait,
        )
        if outcome.reached:
            logger.info("클릭 도달 대상=%s 신호=%s", name, ", ".join(outcome.signals))
            return ClickOutcome(
                reached=True,
                signals=outcome.signals,
                url=outcome.url,
                html=outcome.html,
                requests=outcome.requests,
                navigations=outcome.navigations,
                target=name,
                item_count=len(items),
                skipped=skipped,
            )

    tried = ", ".join(name for name, _ in targets) or "없음"
    return ClickOutcome(
        reached=False,
        failure=DETAIL_UNREACHABLE,
        reason=(
            f"항목 {len(items)}건 중 첫 항목을 눌렀지만 주소·새 탭·요청·본문 넷 다 그대로다. "
            f"누른 것: {tried}"
        ),
        target=tried,
        item_count=len(items),
        skipped=skipped,
    )


async def wait_for_items(
    page: ProbePage,
    item_selector: str,
    *,
    sleep: Callable[[float], Awaitable[None]],
    timeout_seconds: float = ITEMS_WAIT_SECONDS,
    poll_seconds: float = ITEMS_POLL_SECONDS,
) -> list[Any]:
    """항목이 실제로 생길 때까지 기다린다. 끝내 없으면 빈 목록이다."""
    waited = 0.0
    while True:
        found = await page.query_selector_all(item_selector)
        if found:
            return list(found)
        if waited >= timeout_seconds:
            return []
        await sleep(poll_seconds)
        waited += poll_seconds


async def _targets(item: Any) -> tuple[list[tuple[str, Any]], tuple[str, ...]]:
    """누를 것들을 순서대로. 항목 컨테이너가 먼저이고 그다음이 항목 안의 `a` 다."""
    targets: list[tuple[str, Any]] = [("항목 컨테이너", item)]
    skipped: list[str] = []

    anchors: list[Any] = []
    with suppress(Exception):
        anchors = list(await item.query_selector_all("a"))

    for index, anchor in enumerate(anchors):
        href = (await _attribute(anchor, "href")).strip()
        onclick = (await _attribute(anchor, "onclick")).strip()
        if is_share_link(href, onclick):
            skipped.append(f"a[{index}] {href or onclick}")
            continue
        if len(targets) > MAX_ANCHORS:
            break
        targets.append((f"a[{index}] href={href or '없음'}", anchor))

    return targets, tuple(skipped)


def is_share_link(href: str, onclick: str) -> bool:
    """눌러도 상세로 가지 않는 `a` 인가.

    현대의 SNS 공유 버튼이 이 모양이다. 누르면 공유 창이 뜨거나 아무 일도 없고, 그 결과를
    "상세에 못 갔다" 로 읽으면 사이트를 등록할 수 없다고 판정하게 된다.
    """
    if href.lower().startswith("javascript:"):
        return True
    return "share" in onclick.lower()


async def _click_and_judge(
    page: ProbePage,
    *,
    context: ProbeContext,
    log: RequestLog,
    target: Any,
    name: str,
    settle_seconds: float,
    sleep: Callable[[float], Awaitable[None]],
) -> ClickOutcome:
    """하나를 누르고 네 신호를 본다. 클릭 자체가 실패하면 신호 없음으로 본다."""
    before_url = page.url
    before_tabs = len(_pages(context))
    before_body = len(await _content(page))
    mark = log.mark()
    navigation_mark = log.navigation_mark()

    try:
        await target.click()
    except Exception as exc:  # 가려져 있거나 사라진 요소다. 다음 대상으로 넘어간다
        logger.info("클릭 실패 대상=%s (%s)", name, type(exc).__name__)
        return ClickOutcome(reached=False, target=name)

    await sleep(settle_seconds)
    await log.drain()

    requests = tuple(log.since(mark))
    navigations = tuple(log.navigations_since(navigation_mark))
    signals: list[str] = []
    url = page.url
    html = await _content(page)

    opened = [tab for tab in _pages(context) if tab is not page]
    if len(_pages(context)) > before_tabs and opened:
        # 새 탭이 상세다. 읽고 닫는다 — 열어 둔 채로 두면 다음 클릭이 어느 탭에서 일어난
        # 것인지 알 수 없다
        signals.append(SIGNAL_TAB)
        tab = opened[-1]
        url = str(getattr(tab, "url", "") or url)
        html = await _content(tab)
        with suppress(Exception):
            await tab.close()

    if url and before_url and url != before_url and SIGNAL_TAB not in signals:
        signals.append(SIGNAL_URL)
    if requests:
        signals.append(SIGNAL_REQUEST)
    if len(html) - before_body >= BODY_GROWTH_CHARS:
        signals.append(SIGNAL_BODY)

    return ClickOutcome(
        reached=bool(signals),
        signals=tuple(signals),
        url=url,
        html=html,
        requests=requests,
        navigations=navigations,
        target=name,
    )


def _pages(context: ProbeContext) -> list[Any]:
    pages = getattr(context, "pages", None)
    return list(pages) if pages else []


async def _content(page: Any) -> str:
    """페이지 본문. 이동 중이라 못 읽으면 빈 문자열이다."""
    try:
        return str(await page.content())
    except Exception:
        return ""


async def _attribute(node: Any, name: str) -> str:
    with suppress(Exception):
        value = await node.get_attribute(name)
        return str(value or "")
    return ""
