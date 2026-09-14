"""한 필드를 여러 자리에서 모으는 경로. 픽스처로만 돈다.

LG 는 모집 부문마다(`recList`), 삼성은 모집 직무마다(`data.items`) 본문이 따로 있다. 첫 칸만
읽으면 나머지 부문의 본문이 그대로 사라지고, 수집 단계에서 사라진 값은 정규화를 다시 돌려도
돌아오지 않는다.

현대는 한 공고의 주요 업무·조직 소개·기타가 서로 다른 필드에 나뉘어 있다. 그쪽은 배열을 훑는
것이 아니라 자리를 여러 개 적어 모은다.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from app.crawler.api_source import build_detail
from app.selector.api_schema import ApiDetailConfig, validate_api_config

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def payload(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def detail_config(fields: dict[str, Any]) -> ApiDetailConfig:
    return validate_api_config(
        {
            "detail": {
                "url": "https://example.test/detail?id={id}",
                "method": "GET",
                "fields": fields,
            }
        }
    ).detail_config()


def test_the_lg_body_holds_every_sector_not_just_the_first() -> None:
    """`recList` 6칸이 다 들어와야 한다. 첫 칸만 읽으면 본문이 6분의 1이다."""
    data = payload("lg-detail-20260825.json")
    everything = detail_config(
        {
            "title": "data.jobNoticesDetail.jobNoticesDetail.jobNoticeName",
            "body": "data.jobNoticesDetail.recList.*.detailContext",
        }
    )
    first_only = detail_config(
        {
            "title": "data.jobNoticesDetail.jobNoticesDetail.jobNoticeName",
            "body": "data.jobNoticesDetail.recList.0.detailContext",
        }
    )

    whole = build_detail(data, everything).fields["body"]
    part = build_detail(data, first_only).fields["body"]

    assert part in whole
    assert len(whole) > len(part)
    sectors = payload("lg-detail-20260825.json")["data"]["jobNoticesDetail"]["recList"]
    for sector in sectors:
        assert sector["detailContext"] in whole


def test_the_html_fragment_in_the_lg_body_is_left_alone() -> None:
    """본문이 HTML 조각이어도 수집이 펴지 않는다. 정규화의 `html_text` 규칙이 편다."""
    config = detail_config(
        {
            "title": "data.jobNoticesDetail.jobNoticesDetail.jobNoticeName",
            "body": "data.jobNoticesDetail.recList.*.detailContext",
        }
    )

    body = build_detail(payload("lg-detail-20260825.json"), config).fields["body"]

    assert "<" in body


def test_the_samsung_body_holds_every_role() -> None:
    """공고 하나에 모집 직무가 12개다. 12개의 업무가 모두 본문에 들어와야 한다."""
    data = payload("samsung-detail-20260825.json")
    config = detail_config(
        {"title": "data.result.title", "body": ["data.items.*.titleKr", "data.items.*.taskKr"]}
    )

    body = build_detail(data, config).fields["body"]

    roles = data["data"]["items"]
    assert len(roles) == 12
    for role in roles:
        assert role["titleKr"] in body
        assert role["taskKr"] in body


def test_the_hyundai_body_gathers_the_places_it_is_split_across() -> None:
    """주요 업무와 조직 소개가 다른 필드다. 한 자리만 읽으면 본문이 반쪽이다."""
    data = payload("hyundai-detail-02800-20260825.json")
    config = detail_config(
        {
            "title": "data.applyInfo.recuNoticeNm",
            "body": ["data.applyInfo.privJdDtl", "data.applyInfo.aboutTeamNtc"],
            "qualifications": ["data.applyInfo.privMustReq", "data.applyInfo.prefReq"],
        }
    )

    fields = build_detail(data, config).fields

    info = data["data"]["applyInfo"]
    assert info["privJdDtl"] in fields["body"]
    assert info["aboutTeamNtc"] in fields["body"]
    assert info["prefReq"] in fields["qualifications"]


def test_an_empty_place_is_skipped_instead_of_leaving_a_hole() -> None:
    """빈 자리는 건너뛴다. 빈 줄만 남은 값이 본문으로 저장되지 않는다."""
    config = detail_config({"title": "name", "body": ["missing", "text", "alsoMissing"]})

    fields = build_detail({"name": "제목", "text": "본문"}, config).fields

    assert fields["body"] == "본문"


def test_a_wildcard_over_something_that_is_not_a_list_reads_as_empty() -> None:
    """배열이라고 적힌 자리가 배열이 아니면 값이 없다. 통째로 문자열로 밀어 넣지 않는다."""
    config = detail_config({"title": "name", "body": "text", "department": "orgs.*.name"})

    fields = build_detail({"name": "제목", "text": "본문", "orgs": {"name": "조직"}}, config).fields

    assert fields["department"] == ""


def _one(payload: Any, path: Any) -> str:
    """필드 하나만 매핑해 그 값이 어떻게 합쳐지는지 본다."""
    config = ApiDetailConfig(
        url="https://x.test/d",
        method="POST",
        body={"id": "{id}"},
        # `title` 은 필수라 같이 준다. 보려는 것은 `body` 가 어떻게 합쳐지는가다
        fields={"title": "item.t", "body": path},
    )
    return build_detail(payload, config).fields["body"]


def test_같은_값이_칸마다_있으면_한_번만_적는다() -> None:
    """한화는 모집 부문이 셋이어도 근무지가 셋 다 `서울` 이라 이으면 `서울 서울 서울` 이 된다."""
    payload = {"item": {"t": "제목", "unitDt": [{"w": "서울"}, {"w": "서울"}, {"w": "서울"}]}}

    assert _one(payload, "item.unitDt.*.w") == "서울"


def test_다른_값은_순서대로_다_적는다() -> None:
    """줄이는 것은 같은 값뿐이다. 부문마다 다른 근무지는 그대로 남는다."""
    payload = {"item": {"t": "제목", "unitDt": [{"w": "서울"}, {"w": "용인"}, {"w": "서울"}]}}

    assert _one(payload, "item.unitDt.*.w") == "서울\n\n용인"
