"""렌더 중 관찰한 응답에서 목록 API 를 집어내는 것 테스트.

픽스처는 2026-08-25 에 실제로 관찰한 응답이다. 카카오와 우아한형제들은 목록을 JSON 으로
그리고, 토스는 같은 자리에서 푸터·배너·헤더만 내보낸다 — 공고 목록은 초기 HTML 에 이미
들어 있다. 세 사이트가 각각 다른 판정을 받아야 한다.

실사이트에 나가지 않는다. 다시 불러 확인하는 경로만 `httpx.MockTransport` 다.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any
from urllib.parse import urljoin

import httpx
import pytest
from bs4 import BeautifulSoup

from app.config import Settings
from app.crawler.fetcher import Fetcher
from app.crawler.parser import ListItem, parse_list
from app.crawler.playwright import ObservedRequest
from app.selector.list_api import confirm_list_path, propose_list_config
from app.selector.schema import ListSelectors

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
ROBOTS = "User-agent: *\nAllow: /\n"

KAKAO_LIST_URL = "https://careers.kakao.com/jobs?part=BUSINESS_SERVICES&company=KAKAO&page=1"
KAKAO_API_URL = (
    "https://careers.kakao.com/public/api/job-list"
    "?skillSet=&part=BUSINESS_SERVICES&company=KAKAO&employeeType=&page=1"
)
KAKAO_SELECTORS = ListSelectors(
    item="ul.list_jobs > a",
    title="h4.tit_jobs",
    link="",
    date="dl.list_info dd",
    company_name="dl.item_subinfo dd",
)

WOOWA_LIST_URL = "https://career.woowayouths.com/recruitment/"
WOOWA_API_URL = (
    "https://career.woowayouths.com/w1/recruits?category=jobGroupCodes%3ABA005010"
    "&recruitCampaignSeq=0&jobGroupCodes=BA005010&page=0&size=21&sort=updateDate%2Cdesc"
)
WOOWA_SELECTORS = ListSelectors(
    item="ul.recruit-type-list > li",
    title="a.title p.fr-view",
    link="a.title",
    date="div.flag-type span",
    company_name="",
)

# 토스 목록 페이지가 렌더되는 동안 실제로 나간 JSON 응답들. 어느 것도 공고 배열이 아니다
TOSS_RESPONSES = (
    ("https://toss.im/api/common/v3/footer-group", "toss-footer-20260825.json"),
    (
        "https://storage-fe.toss.im/homepage/career/event-banner.json",
        "toss-event-banner-20260825.json",
    ),
    ("https://storage-fe.toss.im/homepage/career/header.json", "toss-header-20260825.json"),
)


def observed(url: str, fixture: str) -> ObservedRequest:
    return ObservedRequest(
        method="GET",
        url=url,
        status=200,
        content_type="application/json",
        body=(FIXTURES / fixture).read_text(encoding="utf-8"),
    )


def rendered(
    fixture: str, selectors: ListSelectors, base_url: str
) -> tuple[list[ListItem], list[str]]:
    """렌더된 목록에서 항목과 페이지에 걸린 주소를 뽑는다. 판정이 받는 것과 같은 값이다."""
    html = (FIXTURES / fixture).read_text(encoding="utf-8")
    items = parse_list(html, selectors, base_url).items
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "javascript:")):
            continue
        url = urljoin(base_url, href)
        if url.startswith(("http://", "https://")):
            links.append(url)
    return items, links


def settings() -> Settings:
    return Settings(crawl_delay_seconds=0.0, crawl_max_retries=1)


def fetcher_for(handler: Any) -> Fetcher:
    return Fetcher(settings=settings(), transport=httpx.MockTransport(handler))


def test_카카오_응답에서_목록_API_를_집어낸다() -> None:
    items, links = rendered("kakao-list-20260825.html", KAKAO_SELECTORS, KAKAO_LIST_URL)
    assert len(items) == 11

    path = propose_list_config(
        [
            observed(
                "https://careers.kakao.com/public/api/jobs-attribute",
                "kakao-jobs-attribute-20260825.json",
            ),
            observed(KAKAO_API_URL, "kakao-list-api-20260825.json"),
        ],
        items,
        links,
    )

    assert path.ok is True
    config = path.config()
    assert config.url == KAKAO_API_URL
    assert config.items_path == "jobList"
    assert config.fields["title"] == "jobOfferTitle"
    assert config.id_field == "realId"
    assert config.link_template.startswith("https://careers.kakao.com/jobs/{id}")
    assert path.count == 11


def test_우아한형제들_응답에서도_집어낸다() -> None:
    items, links = rendered("woowa-list-20260825.html", WOOWA_SELECTORS, WOOWA_LIST_URL)
    assert len(items) == 8

    path = propose_list_config(
        [
            observed(
                "https://career.woowayouths.com/w1/job-groups/statistics",
                "woowa-statistics-20260825.json",
            ),
            observed(WOOWA_API_URL, "woowa-list-api-20260825.json"),
        ],
        items,
        links,
    )

    assert path.ok is True
    config = path.config()
    assert config.items_path == "data.list"
    assert config.fields["title"] == "recruitName"
    assert config.id_field == "recruitNumber"
    assert (
        config.link_template
        == "https://career.woowayouths.com/recruitment/{id}/detail?category=jobGroupCodes%3ABA005010"
    )


def test_후보가_없는_응답에서는_빈_결과다() -> None:
    """토스. 목록이 초기 HTML 에 있어 렌더 중 나간 JSON 에는 공고 배열이 없다."""
    items = [
        ListItem(
            index=0,
            title="Server Developer",
            link="https://toss.im/career/job-detail?job_id=1",
            date="",
        ),
        ListItem(
            index=1,
            title="Product Designer",
            link="https://toss.im/career/job-detail?job_id=2",
            date="",
        ),
    ]

    path = propose_list_config([observed(url, name) for url, name in TOSS_RESPONSES], items, [])

    assert path.ok is False
    assert path.api is None
    assert "이 목록을 담은 JSON 응답이 없다" in path.reason


def test_길이만_맞는_배열은_고르지_않는다() -> None:
    """카카오 `jobTypeCountDtoList` 는 항목 수와 길이가 비슷해도 목록이 아니다."""
    items = [
        ListItem(index=0, title="테크", link="https://example.test/jobs/1", date=""),
        ListItem(index=1, title="디자인", link="https://example.test/jobs/2", date=""),
    ]
    payload = {"counts": [{"name": "테크", "n": 8}, {"name": "디자인", "n": 3}]}
    request = ObservedRequest(
        method="GET",
        url="https://example.test/api/counts",
        status=200,
        content_type="application/json",
        body=json.dumps(payload, ensure_ascii=False),
    )

    path = propose_list_config([request], items, [])

    # 제목은 맞지만 항목을 지목할 id 가 어느 주소에도 없다. 주소를 지어내지 않는다
    assert path.ok is False
    assert "id" in path.reason


def test_모든_항목이_같은_주소면_id_로_채택하지_않는다() -> None:
    """공고마다 다른 주소가 나오지 않으면 중복 판정도 소비 측 링크도 무너진다."""
    items = [
        ListItem(index=0, title="첫 공고", link="https://example.test/jobs", date=""),
        ListItem(index=1, title="둘째 공고", link="https://example.test/jobs", date=""),
    ]
    payload = {
        "list": [
            {"id": "A100", "name": "첫 공고"},
            {"id": "A200", "name": "둘째 공고"},
        ]
    }
    request = ObservedRequest(
        method="GET",
        url="https://example.test/api/list",
        status=200,
        content_type="application/json",
        body=json.dumps(payload, ensure_ascii=False),
    )

    path = propose_list_config([request], items, ["https://example.test/jobs/A100?from=A200"])

    assert path.ok is False


@pytest.mark.asyncio
async def test_다시_불러_같은_목록이_오면_채택한다() -> None:
    items, links = rendered("kakao-list-20260825.html", KAKAO_SELECTORS, KAKAO_LIST_URL)
    path = propose_list_config(
        [observed(KAKAO_API_URL, "kakao-list-api-20260825.json")], items, links
    )
    body = (FIXTURES / "kakao-list-api-20260825.json").read_text(encoding="utf-8")

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        return httpx.Response(200, text=body, headers={"content-type": "application/json"})

    client = fetcher_for(handle)
    try:
        confirmation = await confirm_list_path(client, path, items)
    finally:
        await client.aclose()

    assert confirmation.adopted is True
    assert confirmation.count == 11
    assert confirmation.matched == 11


@pytest.mark.asyncio
async def test_브라우저에서만_되는_요청은_채택하지_않는다() -> None:
    """확인 없이 저장하면 등록만 성공하고 이후 실행이 전부 실패한다."""
    items, links = rendered("kakao-list-20260825.html", KAKAO_SELECTORS, KAKAO_LIST_URL)
    path = propose_list_config(
        [observed(KAKAO_API_URL, "kakao-list-api-20260825.json")], items, links
    )

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        return httpx.Response(403, text="forbidden")

    client = fetcher_for(handle)
    try:
        confirmation = await confirm_list_path(client, path, items)
    finally:
        await client.aclose()

    assert confirmation.adopted is False
    assert "부르지 못했다" in confirmation.reason


@pytest.mark.asyncio
async def test_referer_가_있어야_답하는_API_는_그것을_넣어_확인한다() -> None:
    items, links = rendered("kakao-list-20260825.html", KAKAO_SELECTORS, KAKAO_LIST_URL)
    path = propose_list_config(
        [observed(KAKAO_API_URL, "kakao-list-api-20260825.json")], items, links
    )
    body = (FIXTURES / "kakao-list-api-20260825.json").read_text(encoding="utf-8")
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        referer = request.headers.get("referer", "")
        seen.append(referer)
        if not referer:
            return httpx.Response(403, text="forbidden")
        return httpx.Response(200, text=body, headers={"content-type": "application/json"})

    client = fetcher_for(handle)
    try:
        first = await confirm_list_path(client, path, items)
        with_referer = path.with_referer(KAKAO_LIST_URL)
        second = await confirm_list_path(client, with_referer, items)
    finally:
        await client.aclose()

    assert first.adopted is False
    assert second.adopted is True
    assert seen == ["", KAKAO_LIST_URL]
    # User-Agent 는 설정에 담기지 않는다. 이름은 공용 fetch 클라이언트가 정한다
    assert "user-agent" not in {name.lower() for name in with_referer.config().headers}
