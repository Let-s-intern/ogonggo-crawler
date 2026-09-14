"""쪽을 끝까지 넘겨 목록을 다 받는지 본다. 저장한 쪽별 픽스처로만 돈다.

한화는 20건씩 4쪽 68건이고 `data.hasNext` 가 마지막에 false 가 된다. 삼성은 9+7 = 16건이고
총 쪽 수가 응답 안 `input.divCnt[data-max]` 에 있다. 마지막 쪽을 판정하는 법이 서로 다르고,
둘 다 담을 수 있어야 한다 (`.claude/tasks/done/fill-body/tasks-fill-body-push4.md`).

쪽 사이에도 호스트 딜레이가 지켜지는지, 끝나지 않는 `hasNext` 에 상한이 걸리는지도 여기서 본다.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Callable

import httpx
import pytest

from app.config import Settings
from app.crawler.api_source import fetch_list
from app.crawler.fetcher import Fetcher
from app.crawler.parser import SelectorMissError
from app.selector.api_schema import ApiConfigError, validate_api_config

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
HANWHA_PAGES = [
    (FIXTURES / f"hanwha-list-p{index}-20260825.json").read_text(encoding="utf-8")
    for index in range(4)
]
SAMSUNG_PAGES = [
    (FIXTURES / f"samsung-list-p{index}-20260825.html").read_text(encoding="utf-8")
    for index in (1, 2)
]

ROBOTS = "User-agent: *\nAllow: /\n"
DELAY_SECONDS = 3.0

HANWHA = validate_api_config(
    {
        "list": {
            "url": "https://hwadm.hanwhain.com/new-backend/portal/api/rcRecruit/search-rcrt",
            "method": "POST",
            "body": {"langCd": "ko", "searchText": "", "rtNrcrtYn": "", "rtCarrYn": ""},
            "items_path": "data.list",
            "fields": {"title": "rtNm", "date": "rtAcptEndDttm", "company_name": "sdNm"},
            "id_field": "rtSeq",
            "link_template": "https://www.hanwhain.com/web/recruit/notice/detail?rtSeq={id}",
            "pagination": {
                "param": "page",
                "start": 0,
                "max_pages": 10,
                "has_next": "data.hasNext",
            },
        }
    }
).list_config()

SAMSUNG = validate_api_config(
    {
        "list": {
            "url": "https://www.samsungcareers.com/hr/list.data",
            "method": "POST",
            "body": {"currentPageNo": 1, "intNo": 0, "strVal": ""},
            "body_format": "form",
            "response": "html",
            "items_path": "li",
            "fields": {
                "title": "h3.title",
                "date": "span.period",
                "company_name": "p.company",
            },
            "id_field": "a[data-value]@data-value|digits",
            "link_template": "https://www.samsungcareers.com/recruit/detail.data?seqno={id}",
            "pagination": {
                "param": "currentPageNo",
                "start": 1,
                "max_pages": 10,
                "total_pages_selector": "input.divCnt",
                "total_pages_attribute": "data-max",
            },
        }
    }
).list_config()


class FakeClock:
    """sleep 이 불릴 때만 흐르는 시계. 실제로 기다리지 않고 딜레이를 확인한다."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def client_for(
    handler: Callable[[httpx.Request], httpx.Response], clock: FakeClock | None = None
) -> Fetcher:
    ticker = clock or FakeClock()
    return Fetcher(
        settings=Settings(crawl_delay_seconds=DELAY_SECONDS, crawl_max_retries=1),
        transport=httpx.MockTransport(handler),
        clock=ticker.time,
        sleep=ticker.sleep,
    )


def hanwha_site(seen: list[dict[str, object]]) -> Callable[[httpx.Request], httpx.Response]:
    """본문의 `page` 에 맞는 쪽을 돌려준다. 범위를 넘으면 빈 목록이다."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        body = json.loads(request.content.decode())
        seen.append(body)
        page = int(body.get("page", 0))
        if page >= len(HANWHA_PAGES):
            return httpx.Response(200, text='{"data": {"list": [], "hasNext": false}}')
        return httpx.Response(200, text=HANWHA_PAGES[page])

    return handle


def samsung_site(seen: list[str]) -> Callable[[httpx.Request], httpx.Response]:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        body = request.content.decode()
        seen.append(body)
        number = 2 if "currentPageNo=2" in body else 1
        return httpx.Response(200, text=SAMSUNG_PAGES[number - 1])

    return handle


async def test_hanwha_collects_sixty_eight_over_four_pages() -> None:
    """`hasNext` 가 false 가 될 때까지 넘긴다. 68건이 다 모여야 한다."""
    seen: list[dict[str, object]] = []
    client = client_for(hanwha_site(seen))

    result = await fetch_list(client, HANWHA)
    await client.aclose()

    assert [body["page"] for body in seen] == [0, 1, 2, 3]
    assert result.matched == 68
    assert len(result.items) == 68
    assert len({item.link for item in result.items}) == 68
    assert [item.index for item in result.items[:3]] == [0, 1, 2]
    assert result.items[-1].index == 67


async def test_hanwha_does_not_ask_for_a_page_after_the_last_one() -> None:
    """마지막 쪽에서 멈춘다. 없는 쪽을 한 번 더 물어보지 않는다."""
    seen: list[dict[str, object]] = []
    client = client_for(hanwha_site(seen))

    await fetch_list(client, HANWHA)
    await client.aclose()

    assert len(seen) == 4


async def test_the_host_delay_holds_between_pages() -> None:
    """쪽을 연달아 때리지 않는다. 쪽 사이마다 딜레이가 실제로 걸린다."""
    clock = FakeClock()
    client = client_for(hanwha_site([]), clock)

    await fetch_list(client, HANWHA)
    await client.aclose()

    # robots 1회 + 목록 4쪽. 첫 요청을 뺀 나머지 앞에 딜레이가 붙는다
    assert len(clock.slept) == 4
    assert all(seconds == pytest.approx(DELAY_SECONDS) for seconds in clock.slept)


async def test_samsung_collects_sixteen_over_two_pages() -> None:
    """총 쪽 수를 응답에서 읽어 그만큼만 돈다."""
    seen: list[str] = []
    client = client_for(samsung_site(seen))

    result = await fetch_list(client, SAMSUNG)
    await client.aclose()

    assert len(seen) == 2
    assert "currentPageNo=1" in seen[0]
    assert "currentPageNo=2" in seen[1]
    assert result.matched == 16
    assert len(result.items) == 16
    assert len({item.detail_key for item in result.items}) == 16


async def test_an_endless_has_next_stops_at_the_cap() -> None:
    """`hasNext` 가 끝나지 않는 응답에 걸려도 상한에서 멈춘다."""
    calls: list[int] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        calls.append(1)
        return httpx.Response(200, text=HANWHA_PAGES[0])

    client = client_for(handle)
    result = await fetch_list(client, HANWHA)
    await client.aclose()

    assert len(calls) == 10
    assert len(result.items) == 200


async def test_an_empty_first_page_is_still_a_failure() -> None:
    """첫 쪽부터 0건인 것은 쪽 넘김의 끝이 아니라 목록을 못 읽은 것이다."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        return httpx.Response(200, text='{"data": {"list": [], "hasNext": true}}')

    client = client_for(handle)
    with pytest.raises(SelectorMissError):
        await fetch_list(client, HANWHA)
    await client.aclose()


def test_two_stop_rules_at_once_are_refused() -> None:
    """어느 쪽으로 멈춘 것인지 알 수 없는 설정은 저장 전에 거절한다."""
    with pytest.raises(ApiConfigError) as caught:
        validate_api_config(
            {
                "list": {
                    "url": "https://example.test/jobs",
                    "items_path": "data.list",
                    "fields": {"title": "name"},
                    "id_field": "id",
                    "link_template": "https://example.test/jobs/{id}",
                    "pagination": {
                        "param": "page",
                        "has_next": "data.hasNext",
                        "total_pages_selector": "input.divCnt",
                        "total_pages_attribute": "data-max",
                    },
                }
            }
        )

    assert caught.value.reason == "unknown_field"


def test_a_page_cap_beyond_the_limit_is_refused() -> None:
    """쪽마다 요청이 하나씩 나간다. 설정으로도 절대 상한 위로는 못 올린다."""
    with pytest.raises(ApiConfigError):
        validate_api_config(
            {
                "list": {
                    "url": "https://example.test/jobs",
                    "items_path": "data.list",
                    "fields": {"title": "name"},
                    "id_field": "id",
                    "link_template": "https://example.test/jobs/{id}",
                    "pagination": {"param": "page", "max_pages": 500},
                }
            }
        )
