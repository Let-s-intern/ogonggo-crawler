"""JSON API 수집 경로. LG 응답 픽스처로만 돌고 실사이트에 나가지 않는다.

픽스처는 2026-08-24 에 받은 실제 응답 전문이다.

- `tests/fixtures/lg-list-api-20260824.json` — 목록 83건
- `tests/fixtures/lg-detail-api-20260824.json` — 공고 1002029 의 상세

요청이 공용 fetch 클라이언트를 지나는지도 여기서 본다. `httpx.MockTransport` 를 끼운 진짜
`Fetcher` 를 쓰기 때문에, robots 확인·딜레이·User-Agent 가 실제로 도는 그 경로다.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.crawler.api_source import (
    build_detail,
    build_items,
    fetch_detail,
    fetch_list,
    payload_source_text,
)
from app.crawler.fetcher import Fetcher
from app.crawler.parser import FieldParseError, SelectorMissError
from app.selector.api_schema import validate_api_config

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
LIST_PAYLOAD = json.loads((FIXTURES / "lg-list-api-20260824.json").read_text(encoding="utf-8"))
DETAIL_PAYLOAD = json.loads((FIXTURES / "lg-detail-api-20260824.json").read_text(encoding="utf-8"))
CONFIG = validate_api_config(
    json.loads((FIXTURES / "lg-api-config-20260824.json").read_text(encoding="utf-8"))
)
LIST_CONFIG = CONFIG.list_config()
DETAIL_CONFIG = CONFIG.detail_config()

ROBOTS = "User-agent: *\nAllow: /\n"


def settings() -> Settings:
    """딜레이 0. 픽스처를 돌리는 시험이 실제로 기다릴 이유가 없다."""
    return Settings(crawl_delay_seconds=0.0, crawl_max_retries=1)


def fetcher_for(handler: Any) -> Fetcher:
    return Fetcher(settings=settings(), transport=httpx.MockTransport(handler))


def api_handler(
    seen: list[httpx.Request], *, list_body: str | None = None, status: int = 200
) -> Any:
    """LG 의 두 endpoint 를 픽스처로 답한다. robots 는 전부 허용이다."""

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        if request.url.path.endswith("retrieveJobNoticesList"):
            body = list_body if list_body is not None else json.dumps(LIST_PAYLOAD)
            return httpx.Response(status, text=body, headers={"content-type": "application/json"})
        if request.url.path.endswith("retrieveJobNoticesDetail"):
            return httpx.Response(
                status,
                text=json.dumps(DETAIL_PAYLOAD),
                headers={"content-type": "application/json"},
            )
        return httpx.Response(404, text="not found")

    return handle


async def test_the_list_api_yields_every_posting() -> None:
    """83건이 그대로 항목이 된다. 0건이면 실패지, 신규 없음이 아니다."""
    seen: list[httpx.Request] = []
    client = fetcher_for(api_handler(seen))

    result = await fetch_list(client, LIST_CONFIG)
    await client.aclose()

    assert result.matched == 83
    assert len(result.items) == 83
    assert result.failures == []


async def test_the_request_goes_out_as_the_configured_post() -> None:
    """설정에 적은 메서드와 본문 그대로 나간다. robots 도 먼저 확인한다."""
    seen: list[httpx.Request] = []
    client = fetcher_for(api_handler(seen))

    await fetch_list(client, LIST_CONFIG)
    await client.aclose()

    robots, call = seen
    assert robots.url.path == "/robots.txt"
    assert (call.method, str(call.url)) == ("POST", LIST_CONFIG.url)
    assert json.loads(call.content) == LIST_CONFIG.body
    assert call.headers["user-agent"] == client.user_agent


async def test_each_posting_gets_its_own_source_url() -> None:
    """`link_template` 이 공고마다 다른 주소를 만든다. 같으면 중복 판정이 무너진다."""
    client = fetcher_for(api_handler([]))

    result = await fetch_list(client, LIST_CONFIG)
    await client.aclose()

    links = [item.link for item in result.items]
    assert len(set(links)) == len(links)
    assert links[0] == "https://careers.lg.com/apply/detail?id=1002029"
    assert all(link.startswith("https://careers.lg.com/apply/detail?id=") for link in links)


async def test_the_list_carries_the_affiliate_name() -> None:
    """LG 는 계열사 공고가 섞여 있다. 회사명이 공고마다 달라야 정규화가 그것을 쓴다."""
    client = fetcher_for(api_handler([]))

    result = await fetch_list(client, LIST_CONFIG)
    await client.aclose()

    companies = {item.company for item in result.items}
    assert "LG유플러스" in companies
    assert len(companies) > 1
    assert "" not in companies


async def test_the_list_fields_are_filled() -> None:
    first = build_items(LIST_PAYLOAD, LIST_CONFIG).items[0]

    assert first.title == "[정보보안센터] IT보안 담당자 경력채용"
    assert first.date == "2026.08.30 23:00"
    assert first.company == "LG유플러스"
    # 상세 API 에 넘길 id 다. 목록이 API 라 응답에서 그대로 온다
    assert first.detail_key == "1002029"


async def test_the_detail_api_fills_the_fields() -> None:
    """상세 응답의 경로가 그대로 필드가 된다."""
    seen: list[httpx.Request] = []
    client = fetcher_for(api_handler(seen))

    detail = await fetch_detail(client, DETAIL_CONFIG, "1002029")
    await client.aclose()

    assert detail.fields["title"] == "[정보보안센터] IT보안 담당자 경력채용"
    assert detail.fields["company"] == "LG유플러스"
    assert detail.fields["department"] == "정보보안센터"
    assert detail.fields["deadline"] == "2026.08.30 23:00"
    assert detail.missing == []


async def test_the_detail_request_carries_the_id_as_a_number() -> None:
    """LG 는 숫자 id 를 받는다. 문자열로 보내면 빈 응답이 온다."""
    seen: list[httpx.Request] = []
    client = fetcher_for(api_handler(seen))

    await fetch_detail(client, DETAIL_CONFIG, "1002029")
    await client.aclose()

    call = seen[-1]
    assert json.loads(call.content) == {"jobNoticeId": 1002029}


def test_the_body_keeps_the_html_fragment() -> None:
    """`detailContext` 는 HTML 조각이다. 텍스트로 펴는 것은 정규화의 일이다."""
    detail = build_detail(DETAIL_PAYLOAD, DETAIL_CONFIG)

    assert detail.fields["body"].startswith("<!--StartFragment-->")
    assert "<p" in detail.fields["body"]
    assert "<p" in detail.fields["requirements"]


def test_an_empty_array_is_a_failure_not_an_empty_success() -> None:
    """200 인데 배열이 비었다. 마크업이 바뀐 사이트와 공고 없는 사이트를 섞지 않는다."""
    payload = {"status": "S", "data": {"jobNoticeList": []}}

    with pytest.raises(SelectorMissError) as caught:
        build_items(payload, LIST_CONFIG)

    assert caught.value.error_class == "selector_miss"
    assert "빈 배열" in str(caught.value)


def test_a_missing_items_path_is_a_selector_miss() -> None:
    """경로가 안 잡혔다. 재시도로는 풀리지 않으므로 셀렉터 미스와 같은 분류다."""
    payload = {"status": "S", "data": {"jobList": [{"jobNoticeId": 1}]}}

    with pytest.raises(SelectorMissError) as caught:
        build_items(payload, LIST_CONFIG)

    assert caught.value.error_class == "selector_miss"
    assert "data.jobNoticeList" in str(caught.value)


def test_a_path_that_is_not_an_array_is_a_parse_failure() -> None:
    """경로는 잡혔는데 배열이 아니다. 한 단계 얕거나 깊게 짚은 것이다."""
    payload = {"status": "S", "data": {"jobNoticeList": {"total": 83}}}

    with pytest.raises(FieldParseError) as caught:
        build_items(payload, LIST_CONFIG)

    assert caught.value.error_class == "parse"
    assert "배열이 아니다" in str(caught.value)


async def test_a_response_that_is_not_json_is_a_parse_failure() -> None:
    """전송은 성공했다. 200 으로 HTML 이 오면 endpoint 를 잘못 짚은 것이다."""
    client = fetcher_for(api_handler([], list_body="<html>점검 중</html>"))

    with pytest.raises(FieldParseError) as caught:
        await fetch_list(client, LIST_CONFIG)
    await client.aclose()

    assert caught.value.error_class == "parse"
    assert "JSON 이 아니다" in str(caught.value)


def test_an_item_without_a_title_is_counted_as_a_field_failure() -> None:
    """항목 하나가 비어도 나머지는 남는다. 실패한 항목만 사유로 적힌다."""
    payload = {
        "data": {
            "jobNoticeList": [
                {"jobNoticeId": 1, "jobNoticeName": "", "companyName": "LG"},
                {"jobNoticeId": 2, "jobNoticeName": "제목", "companyName": "LG"},
            ]
        }
    }

    result = build_items(payload, LIST_CONFIG)

    assert [item.title for item in result.items] == ["제목"]
    assert [(f.index, f.field) for f in result.failures] == [(0, "title")]
    assert result.matched == 2


def test_items_without_an_id_cannot_become_postings() -> None:
    """id 가 없으면 상세로도 못 가고 주소도 못 만든다. 전부 그러면 실행이 실패한다."""
    payload = {"data": {"jobNoticeList": [{"jobNoticeName": "제목"}]}}

    with pytest.raises(FieldParseError) as caught:
        build_items(payload, LIST_CONFIG)

    assert "id" in str(caught.value)


def test_a_detail_missing_the_required_fields_is_a_parse_failure() -> None:
    """제목과 본문이 없으면 적재할 내용이 없다."""
    with pytest.raises(FieldParseError) as caught:
        build_detail({"data": {}}, DETAIL_CONFIG)

    assert caught.value.error_class == "parse"
    assert "title" in str(caught.value)
    assert "body" in str(caught.value)


def test_an_optional_field_that_is_absent_is_reported_as_missing() -> None:
    """필수가 아닌 필드는 비어도 실패가 아니다. 어느 것이 비었는지는 남긴다."""
    payload = json.loads(json.dumps(DETAIL_PAYLOAD))
    payload["data"]["jobNoticesDetail"]["recList"][0]["orgName"] = None

    detail = build_detail(payload, DETAIL_CONFIG)

    assert detail.fields["department"] == ""
    assert detail.missing == ["department"]


def test_the_detail_carries_the_whole_response_as_source_text() -> None:
    result = build_detail(DETAIL_PAYLOAD, DETAIL_CONFIG)

    assert result.source_text.strip()
    assert "<" not in result.source_text


def test_source_text_starts_each_value_with_its_key() -> None:
    """짧은 값은 키 이름이 있어야 무엇인지 안다. 여러 줄인 값은 첫 줄에만 붙는다."""
    payload = {"data": {"ruWorkpl": "본사(서울 63빌딩)", "body": "<p>첫 줄</p><p>둘째 줄</p>"}}

    assert payload_source_text(payload) == "ruWorkpl: 본사(서울 63빌딩)\nbody: 첫 줄\n둘째 줄"


def test_source_text_leaves_codes_and_links_out_but_keeps_dates_and_words() -> None:
    payload = {
        "hidden": "N",
        "status": "SUCCESS",
        "seq": 19463,
        "taskCode": "G12002",
        "opened": True,
        "memo": None,
        "site": "https://example.com/careers",
        "logo": "promotion/20220801_f02.png",
        "endDt": "20260830",
        "endTm": "1705",
        "end": "2026.08.25 15:00",
        "major": "[Finance]",
        "team": "Finance",
    }

    assert payload_source_text(payload) == (
        "endDt: 20260830\nendTm: 1705\nend: 2026.08.25 15:00\nmajor: [Finance]\nteam: Finance"
    )


def test_a_value_whose_first_line_is_dropped_gives_its_key_to_the_next_line() -> None:
    payload = {"rtEct": "https://example.com\n■ 처우 : 내규에 따라 개별 협상"}

    assert payload_source_text(payload) == "rtEct: ■ 처우 : 내규에 따라 개별 협상"


def test_repeated_sentences_are_dropped_only_inside_one_group() -> None:
    """삼성은 직무마다 한글·영어 칸에 같은 자격요건이 있다. 직무 사이의 반복은 남긴다."""
    same = "- 2년 이상 유관경력 보유하신 분"
    payload = {
        "items": [
            {"qlfctKr": same, "qlfctEn": same},
            {"qlfctKr": same, "qlfctEn": same},
        ]
    }

    assert payload_source_text(payload) == f"qlfctKr: {same}\nqlfctKr: {same}"


def test_short_values_are_kept_even_when_repeated() -> None:
    """`서울` 이 두 칸에 있는 것은 문장이 겹친 것이 아니다."""
    payload = {"workPlaceKr": "서울", "locationName": "서울"}

    assert payload_source_text(payload) == "workPlaceKr: 서울\nlocationName: 서울"
