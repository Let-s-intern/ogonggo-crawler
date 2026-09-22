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
from app.selector.list_api import (
    confirm_list_path,
    propose_list_config,
    restore_truncated,
)
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


def test_주소의_일부와만_겹치는_값은_id_로_고르지_않는다() -> None:
    """KT 실측(2026-09-17): 정렬 순서 `sortOrder` 가 공고 번호 앞자리와 겹쳐 id 로 뽑혔다."""
    items = [
        ListItem(index=0, title="첫 공고", link="https://example.test/careers/266550", date=""),
        ListItem(index=1, title="둘째 공고", link="https://example.test/careers/267551", date=""),
    ]
    payload = {
        "data": [
            {"sortOrder": 266, "noticeSn": 266550, "name": "첫 공고"},
            {"sortOrder": 267, "noticeSn": 267551, "name": "둘째 공고"},
        ]
    }
    request = ObservedRequest(
        method="GET",
        url="https://example.test/api/recruit",
        status=200,
        content_type="application/json",
        body=json.dumps(payload, ensure_ascii=False),
    )

    path = propose_list_config(
        [request],
        items,
        ["https://example.test/careers/266550", "https://example.test/careers/267551"],
    )

    assert path.ok is True
    assert path.config().id_field == "noticeSn"
    assert path.config().link_template == "https://example.test/careers/{id}"


def test_링크가_없는_페이지는_눌러서_도착한_주소로_id_를_찾는다() -> None:
    """롯데ON 실측(2026-09-17): 항목에 링크가 없고, 눌러야 `/job_posting/UJFP0dBm` 이 열린다."""
    items = [
        ListItem(index=0, title="광고 상품 & 플랫폼 기획", link="", date="", detail_absent=True),
        ListItem(index=1, title="데이터 엔지니어", link="", date="", detail_absent=True),
    ]
    payload = {
        "count": 2,
        "results": [
            {
                "status": "in_progress",
                "externalTitle": "광고 상품 & 플랫폼 기획",
                "addressKey": "UJFP0dBm",
            },
            {"status": "in_progress", "externalTitle": "데이터 엔지니어", "addressKey": "Q7xk2Lmn"},
        ],
    }
    request = ObservedRequest(
        method="GET",
        url="https://example.test/_backend/recruitments?page=1",
        status=200,
        content_type="application/json",
        body=json.dumps(payload, ensure_ascii=False),
    )

    path = propose_list_config(
        [request], items, [], clicked_url="https://example.test/job_posting/UJFP0dBm"
    )

    assert path.ok is True
    assert path.config().id_field == "addressKey"
    assert path.config().link_template == "https://example.test/job_posting/{id}"


def test_눌러서_얻은_주소와_겹쳐도_항목마다_같은_값이면_id_로_쓰지_않는다() -> None:
    items = [
        ListItem(index=0, title="첫 공고", link="", date="", detail_absent=True),
        ListItem(index=1, title="둘째 공고", link="", date="", detail_absent=True),
    ]
    payload = {
        "results": [
            {"name": "첫 공고", "companyKey": "LOTTEON"},
            {"name": "둘째 공고", "companyKey": "LOTTEON"},
        ]
    }
    request = ObservedRequest(
        method="GET",
        url="https://example.test/api/list",
        status=200,
        content_type="application/json",
        body=json.dumps(payload, ensure_ascii=False),
    )

    path = propose_list_config(
        [request], items, [], clicked_url="https://example.test/LOTTEON/job/1"
    )

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


# HD현대 실측(2026-09-21): 목록 API 는 `x-user-role` 이 없으면 500 이고, 응답은 끝난 공고까지
# 본문째로 담아 1.4MB 다. 관찰 상한(200,000자)을 넘겨 JSON 으로 읽히지 않는다
HD_LIST_URL = "https://recruit.hd.com/kr/mainLayout/apply"
HD_API_URL = "https://recruit.hd.com/api/v1/jobda/getRecruitNoticeList?isPost=true&LANG=KR"
HD_ITEMS = [
    ListItem(
        index=0, title="[HD현대] 26년 하반기 신입사원 채용", link="", date="", detail_absent=True
    ),
    ListItem(
        index=1, title="[HD현대] 26년 하반기 연구직 채용", link="", date="", detail_absent=True
    ),
]
HD_PAYLOAD = {
    "data": [
        {"recruitNoticeSn": 264514, "recruitNoticeName": "[HD현대] 26년 하반기 신입사원 채용"},
        {"recruitNoticeSn": 264865, "recruitNoticeName": "[HD현대] 26년 하반기 연구직 채용"},
    ]
}
HD_BODY = json.dumps(HD_PAYLOAD, ensure_ascii=False)


def hd_observed(*, body: str, truncated: bool) -> ObservedRequest:
    return ObservedRequest(
        method="GET",
        url=HD_API_URL,
        status=200,
        content_type="application/json",
        body=body,
        truncated=truncated,
        request_headers={"x-user-role": "FRONT"},
    )


def test_관찰한_기능성_헤더는_설정에_담긴다() -> None:
    """헤더를 떼고 저장하면 등록만 성공하고 이후 실행이 전부 실패한다."""
    path = propose_list_config(
        [hd_observed(body=HD_BODY, truncated=False)],
        HD_ITEMS,
        [],
        clicked_url="https://recruit.hd.com/kr/mainLayout/applyDetail/264514",
    )

    assert path.ok is True
    assert path.config().headers == {"x-user-role": "FRONT"}
    assert path.config().id_field == "recruitNoticeSn"
    assert path.config().link_template == "https://recruit.hd.com/kr/mainLayout/applyDetail/{id}"


@pytest.mark.asyncio
async def test_잘린_JSON_응답은_다시_받아_채운다() -> None:
    """상한에 잘린 응답은 `json.loads` 가 실패해 "목록 API 가 없다" 로 판정된다."""
    truncated = hd_observed(body=HD_BODY[:40], truncated=True)
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        seen.append(request.headers.get("x-user-role", ""))
        if not seen[-1]:
            return httpx.Response(500, text='{"code":500}')
        return httpx.Response(200, text=HD_BODY, headers={"content-type": "application/json"})

    client = fetcher_for(handle)
    try:
        assert propose_list_config([truncated], HD_ITEMS, []).ok is False
        restored = await restore_truncated(client, [truncated])
    finally:
        await client.aclose()

    assert seen == ["FRONT"]
    assert restored[0].truncated is False
    assert restored[0].body == HD_BODY
    # 채운 뒤에는 같은 응답이 목록으로 읽힌다
    path = propose_list_config(
        restored,
        HD_ITEMS,
        [],
        clicked_url="https://recruit.hd.com/kr/mainLayout/applyDetail/264514",
    )
    assert path.ok is True


@pytest.mark.asyncio
async def test_다시_받지_못하면_잘린_채로_둔다() -> None:
    """막는 자리를 앞당길 뿐이다. 판정 사유는 지금까지와 같다."""
    truncated = hd_observed(body=HD_BODY[:40], truncated=True)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        return httpx.Response(403, text="forbidden")

    client = fetcher_for(handle)
    try:
        restored = await restore_truncated(client, [truncated])
    finally:
        await client.aclose()

    assert restored == [truncated]


@pytest.mark.asyncio
async def test_잘리지_않은_응답은_다시_부르지_않는다() -> None:
    """등록 한 번이 사이트를 이유 없이 더 때리지 않게 한다."""
    whole = hd_observed(body=HD_BODY, truncated=False)
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text=ROBOTS)

    client = fetcher_for(handle)
    try:
        restored = await restore_truncated(client, [whole])
    finally:
        await client.aclose()

    assert restored == [whole]
    assert calls == []


def _nhn_request() -> ObservedRequest:
    payload = {
        "result": [
            {"id": "4422617909897514692", "name": "백엔드 개발 인턴 (체험형)"},
            {"id": "4405976305114575784", "name": "[서울] 고객상담(정규직)_동행복권"},
        ]
    }
    return ObservedRequest(
        method="GET",
        url="https://example.test/v1/job-postings?page=0&size=30",
        status=200,
        content_type="application/json",
        body=json.dumps(payload, ensure_ascii=False),
    )


def test_화면이_제목_앞에_회사를_붙여도_목록_API_를_찾는다() -> None:
    """NHN 실측(2026-09-22): 화면 제목 앞의 `[NHN] ` 이 응답의 `name` 에는 없다."""
    items = [
        ListItem(index=0, title="[NHN] 백엔드 개발 인턴 (체험형)", link="", date=""),
        ListItem(index=1, title="[NHN Service] [서울] 고객상담(정규직)_동행복권", link="", date=""),
    ]

    path = propose_list_config(
        [_nhn_request()],
        items,
        [],
        clicked_url="https://example.test/recruits/4422617909897514692?type=list",
    )

    assert path.ok is True
    assert path.config().fields["title"] == "name"
    assert path.config().id_field == "id"


def test_짧은_값이_제목에_들어_있다고_같은_공고로_보지_않는다() -> None:
    items = [
        ListItem(index=0, title="2026 하반기 신입 채용 [개발]", link="", date=""),
        ListItem(index=1, title="2026 하반기 신입 채용 [디자인]", link="", date=""),
    ]
    payload = {"result": [{"id": "1001", "name": "신입"}, {"id": "1002", "name": "채용"}]}
    request = ObservedRequest(
        method="GET",
        url="https://example.test/v1/postings",
        status=200,
        content_type="application/json",
        body=json.dumps(payload, ensure_ascii=False),
    )

    assert propose_list_config([request], items, []).ok is False


def test_날짜는_표기가_달라도_같은_날이면_마감일_칸으로_짝을_짓는다() -> None:
    """NHN 실측(2026-09-22): 화면 `~ 26.09.27`, 응답 `postingEndDatetime: 2026-09-27T23:59:00`."""
    items = [
        ListItem(index=0, title="[NHN] 백엔드 개발 인턴 (체험형)", link="", date="~ 26.09.27"),
        ListItem(
            index=1,
            title="[NHN Service] [서울] 고객상담(정규직)_동행복권",
            link="",
            date="2026.09.14 ~ 2026.10.05",
        ),
    ]
    payload = {
        "result": [
            {
                "id": "4422617909897514692",
                "name": "백엔드 개발 인턴 (체험형)",
                "postingStaDatetime": "2026-09-16T16:00:00",
                "postingEndDatetime": "2026-09-27T23:59:00",
            },
            {
                "id": "4405976305114575784",
                "name": "[서울] 고객상담(정규직)_동행복권",
                "postingStaDatetime": "2026-09-14T09:00:00",
                "postingEndDatetime": "2026-10-05T23:59:00",
            },
        ]
    }
    request = ObservedRequest(
        method="GET",
        url="https://example.test/v1/job-postings?page=0&size=30",
        status=200,
        content_type="application/json",
        body=json.dumps(payload, ensure_ascii=False),
    )

    path = propose_list_config(
        [request],
        items,
        [],
        clicked_url="https://example.test/recruits/4422617909897514692?type=list",
    )

    assert path.ok is True
    assert path.config().fields["date"] == "postingEndDatetime"


def test_화면에서_날짜를_못_읽으면_마감을_뜻하는_키를_쓴다() -> None:
    """NHN 실측(2026-09-22): 모델이 목록 날짜 셀렉터를 비웠다."""
    items = [
        ListItem(index=0, title="[NHN] 백엔드 개발 인턴 (체험형)", link="", date=""),
        ListItem(index=1, title="[NHN Service] [서울] 고객상담(정규직)_동행복권", link="", date=""),
    ]
    payload = {
        "result": [
            {
                "id": "4422617909897514692",
                "name": "백엔드 개발 인턴 (체험형)",
                "postingStaDatetime": "2026-09-16T16:00:00",
                "postingEndDatetime": "2026-09-27T23:59:00",
            },
            {
                "id": "4405976305114575784",
                "name": "[서울] 고객상담(정규직)_동행복권",
                "postingStaDatetime": "2026-09-14T09:00:00",
                "postingEndDatetime": "2026-10-05T23:59:00",
            },
        ]
    }
    request = ObservedRequest(
        method="GET",
        url="https://example.test/v1/job-postings?page=0&size=30",
        status=200,
        content_type="application/json",
        body=json.dumps(payload, ensure_ascii=False),
    )

    path = propose_list_config(
        [request],
        items,
        [],
        clicked_url="https://example.test/recruits/4422617909897514692?type=list",
    )

    assert path.config().fields["date"] == "postingEndDatetime"
    assert path.config().date_is_deadline is True
