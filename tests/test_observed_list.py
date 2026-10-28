"""봇 확인이 정적 요청을 전부 막는 사이트의 목록을 렌더 중 관찰한 응답으로 읽는다.

클래스101 실측(2026-10-28): 목록 페이지, 목록 API, 상세 페이지가 모두 `429` 에
`x-vercel-mitigated: challenge` 였다. 목록 API 를 다시 불러 확인할 수 없으니 브라우저가 이미
받은 응답으로 확인하고, 실행도 그 응답을 읽는다 (`app/crawler/api_source.py` 의
`ObservedResponses`).

실사이트에도 브라우저에도 나가지 않는다. 응답은 실측한 모양을 줄여 옮긴 것이다.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.crawler.api_source import ObservedResponses
from app.crawler.collect import API, PLAYWRIGHT, open_collectors
from app.crawler.fetcher import Fetcher, FetchResult
from app.crawler.parser import FieldParseError, ListItem, SelectorMissError
from app.crawler.playwright import ObservedRequest
from app.selector.api_schema import ApiConfig, ApiConfigError, validate_api_config
from app.selector.list_api import confirm_list_path, propose_list_config
from app.selector.schema import validate_selectors

LIST_URL = "https://jobs.example.test/career"
API_URL = (
    "https://jobs.example.test/_backend/identity-access/homepage/recruitments"
    "?companyId=c1&page=1&countPerPage=10&order=created_at_desc"
)
CLICKED_URL = "https://jobs.example.test/job_posting/cGcY7nk3"
TITLES = ("[정규직] 브랜드 디자이너", "[정규직] 브랜드 마케터", "[인턴] 콘텐츠 에디터")
KEYS = ("cGcY7nk3", "dvBvZeeE", "zQTN0vnf")

PAYLOAD = {
    "count": 3,
    "results": [
        {"recruitmentId": f"r-{index}", "addressKey": key, "title": title, "status": "in_progress"}
        for index, (key, title) in enumerate(zip(KEYS, TITLES, strict=True))
    ],
}


def observed(url: str = API_URL, *, truncated: bool = False) -> ObservedRequest:
    return ObservedRequest(
        method="GET",
        url=url,
        status=200,
        content_type="application/json",
        body=json.dumps(PAYLOAD, ensure_ascii=False),
        truncated=truncated,
    )


def items() -> list[ListItem]:
    """렌더된 목록. 항목에 링크가 없다 — 누르면 새 탭으로 상세가 열린다."""
    return [ListItem(index=i, title=title, link="", date="") for i, title in enumerate(TITLES)]


def challenged_fetcher() -> Fetcher:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"x-vercel-mitigated": "challenge"}, text="checkpoint")

    return Fetcher(
        settings=Settings(crawl_delay_seconds=0, crawl_max_retries=0),
        transport=httpx.MockTransport(handle),
    )


def proposed() -> Any:
    path = propose_list_config([observed()], items(), clicked_url=CLICKED_URL)
    assert path.ok, path.reason
    return path


@pytest.mark.asyncio
async def test_봇_확인에_막힌_확인은_막혔다고_알린다() -> None:
    client = challenged_fetcher()
    try:
        confirmation = await confirm_list_path(client, proposed(), items())
    finally:
        await client.aclose()

    assert confirmation.adopted is False
    assert confirmation.challenged is True


@pytest.mark.asyncio
async def test_관찰한_응답으로_확인하고_그_페이지를_읽게_저장한다() -> None:
    path = proposed().observed_on(LIST_URL)

    confirmation = await confirm_list_path(ObservedResponses([observed()]), path, items())

    assert confirmation.adopted is True
    assert confirmation.matched == 3
    config = path.config()
    assert config.observe_on == LIST_URL
    assert config.pagination is None
    assert config.link_template == "https://jobs.example.test/job_posting/{id}"


def test_observe_on_은_저장했다_읽어도_남는다() -> None:
    config = proposed().observed_on(LIST_URL).api

    again = validate_api_config(json.loads(config.to_json()))

    assert again.list is not None
    assert again.list.observe_on == LIST_URL


def test_observe_on_이_http_주소가_아니면_거절한다() -> None:
    data = json.loads(proposed().api.to_json())
    data["list"]["observe_on"] = "career"

    with pytest.raises(ApiConfigError):
        validate_api_config(data)


@pytest.mark.asyncio
async def test_관찰_응답은_쿼리가_달라도_같은_주소로_본다() -> None:
    responses = ObservedResponses([observed(API_URL.replace("page=1", "page=1&externalTitle="))])

    result = await responses.request(API_URL)

    assert json.loads(result.text)["count"] == 3


@pytest.mark.asyncio
async def test_페이지가_부르지_않은_API_는_목록을_못_찾은_것이다() -> None:
    responses = ObservedResponses([observed("https://jobs.example.test/_backend/other")])

    with pytest.raises(SelectorMissError):
        await responses.request(API_URL)


@pytest.mark.asyncio
async def test_잘린_관찰_응답은_읽지_않는다() -> None:
    with pytest.raises(FieldParseError):
        await ObservedResponses([observed(truncated=True)]).request(API_URL)


class StubRenderer:
    """`fetch_observed` 만 하는 렌더러 대역. 어느 페이지를 그렸는지 기록한다."""

    def __init__(self) -> None:
        self.rendered: list[str] = []
        self.closed = False

    async def fetch_observed(self, url: str) -> tuple[FetchResult, list[ObservedRequest]]:
        self.rendered.append(url)
        return FetchResult(url=url, status_code=200, text="<html></html>"), [observed()]

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_실행은_목록_페이지를_그려_관찰한_응답에서_목록을_읽는다() -> None:
    config = proposed().observed_on(LIST_URL).api
    assert isinstance(config, ApiConfig)
    stub = StubRenderer()
    selectors = validate_selectors(
        {
            "list": {"item": "div", "title": "h1", "link": "", "date": ""},
            "detail": {
                "title": "h1",
                "body": "div",
                "qualifications": "",
                "recruitment_end_at": "",
                "department": "",
            },
        }
    )

    async with open_collectors(
        list_mode=API,
        detail_mode=PLAYWRIGHT,
        list_url=LIST_URL,
        selectors=selectors,
        fetcher=challenged_fetcher(),
        api_config=config,
        renderer=lambda _: stub,  # type: ignore[arg-type,return-value]
    ) as collectors:
        result = await collectors.list.collect()

    assert stub.rendered == [LIST_URL]
    assert stub.closed is True
    assert [item.title for item in result.items] == list(TITLES)
    assert [item.link for item in result.items] == [
        f"https://jobs.example.test/job_posting/{key}" for key in KEYS
    ]
