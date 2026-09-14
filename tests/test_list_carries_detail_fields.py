"""목록이 상세 칸의 값을 들고 있을 때 그것을 나른다 (2.3).

카카오 목록 API 는 직군·근무지·모집인원·주요 업무·전형 절차를 항목마다 담아 주는데, 상세
문서에는 그것들이 `◆ 업무내용` 처럼 한 덩어리 안에 섞여 있어 셀렉터로 갈라낼 수 없다.
읽지 않으면 그 값들은 수집 단계에서 사라지고, **매핑하지 않은 값은 저장되지 않으므로** 다시
얻을 길이 없다 (`.claude/tasks/memos/보류/split-body/prd-split-body.md`).

여기서 보는 것은 셋이다 — 목록 설정이 상세 칸 이름을 받는가, 그 값이 항목에 실리는가,
`_record` 에서 **상세가 이기고 목록은 상세가 비었을 때만** 쓰이는가.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from app.crawler.api_source import build_items
from app.crawler.parser import ListItem
from app.crawler.runner import _record
from app.selector.api_schema import ApiConfigError, validate_api_config
from app.selector.schema import DETAIL_FIELDS

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

KAKAO_LIST: dict[str, Any] = {
    "list": {
        "url": "https://careers.kakao.com/public/api/job-list?page=1",
        "method": "GET",
        "body": {},
        "items_path": "jobList",
        "fields": {
            "title": "jobOfferTitle",
            "company_name": "companyName",
            "job_category": "jobPartName",
            "employment_type": "employeeTypeName",
            "region": "locationName",
            "headcount": "displayRecruitCount",
            "responsibilities": "workContentDesc",
            "qualifications": "qualification",
            "hiring_process": "jobOfferProcessDesc",
        },
        "id_field": "realId",
        "link_template": "https://careers.kakao.com/jobs/{id}",
    }
}


def payload(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def empty_detail() -> dict[str, str]:
    """상세가 아무것도 못 읽은 자리. 목록이 채우는지를 보려면 이쪽이 비어야 한다."""
    return dict.fromkeys(DETAIL_FIELDS, "")


def test_the_list_section_accepts_detail_column_names() -> None:
    config = validate_api_config(KAKAO_LIST)

    assert config.list is not None
    assert config.list.fields["responsibilities"] == "workContentDesc"


def test_a_name_that_is_in_neither_schema_is_still_refused() -> None:
    """넓어진 것은 상세 칸 이름까지다. 아무 이름이나 받는다는 뜻이 아니다."""
    broken = json.loads(json.dumps(KAKAO_LIST))
    broken["list"]["fields"]["salary"] = "payAmount"

    with pytest.raises(ApiConfigError) as caught:
        validate_api_config(broken)

    assert caught.value.reason == "unknown_field"


def test_the_kakao_list_carries_the_columns_its_detail_cannot_split() -> None:
    config = validate_api_config(KAKAO_LIST).list_config()

    result = build_items(payload("kakao-list-api-20260825.json"), config)

    first = result.items[0]
    assert first.extra["job_category"] == "서비스비즈"
    assert first.extra["employment_type"] == "정규직"
    assert first.extra["region"] == "판교"
    assert first.extra["responsibilities"].startswith("- 카카오비즈니스와 외부 제휴사 간")
    assert first.extra["hiring_process"].startswith("서류전형")
    # 목록 자신의 세 값은 `extra` 에 들어가지 않는다. 이미 제 자리가 있다
    assert "title" not in first.extra
    assert "company_name" not in first.extra


def test_an_empty_value_is_not_carried() -> None:
    """빈 문자열을 실어 두면 "목록도 비었다" 와 "목록이 안 준다" 가 같은 모양이 된다."""
    config = validate_api_config(KAKAO_LIST).list_config()

    result = build_items(payload("kakao-list-api-20260825.json"), config)

    assert all(value.strip() for item in result.items for value in item.extra.values())


def test_the_detail_wins_when_both_have_a_value() -> None:
    item = ListItem(
        index=0,
        title="목록 제목",
        link="https://careers.kakao.com/jobs/P-14503",
        date="",
        extra={"responsibilities": "목록이 준 업무", "region": "판교"},
    )
    detail = empty_detail() | {
        "title": "상세 제목",
        "body": "본문",
        "responsibilities": "상세가 준 업무",
    }

    record = _record(item, detail)

    assert record["responsibilities"] == "상세가 준 업무"
    assert record["region"] == "판교"


def test_a_column_neither_side_gives_stays_empty() -> None:
    """빈 칸을 채우려고 뜻이 다른 값을 옮겨 오지 않는다."""
    item = ListItem(index=0, title="제목", link="https://example.test/1", date="")
    detail = empty_detail() | {"title": "제목", "body": "본문"}

    record = _record(item, detail)

    assert record["preferred_qualifications"] == ""
    assert record["department"] == ""
