"""셀렉터 JSON 스키마 검증 테스트. 정상 1건과 실패 사유별 분류를 단언한다."""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.selector.schema import (
    SPLIT_DETAIL_FIELDS,
    SelectorSchemaError,
    parse_selectors,
    validate_selectors,
)

VALID: dict[str, Any] = {
    "list": {
        "item": "ol.list-recent-jobs > li",
        "title": "h2.listing-company a",
        "link": "h2.listing-company a",
        "date": "span.listing-posted time",
    },
    "detail": {
        "title": "h1.listing-company .company-name",
        "body": "div.job-description",
        "qualifications": "div.job-description ul.simple",
        "recruitment_end_at": "",
        "department": "",
    },
}


def stored(payload: dict[str, Any]) -> dict[str, Any]:
    """저장되는 모양. 선택 필드는 안 적어도 빈 문자열로 채워져 저장된다."""
    filled = json.loads(json.dumps(payload))
    filled["list"].setdefault("company_name", "")
    filled["list"].setdefault("link_template", "")
    filled["detail"].setdefault("company_name", "")
    for name in SPLIT_DETAIL_FIELDS:
        filled["detail"].setdefault(name, "")
    return filled


def test_valid_payload_passes() -> None:
    selectors = validate_selectors(VALID)

    assert selectors.list.item == "ol.list-recent-jobs > li"
    assert selectors.detail.recruitment_end_at == ""
    assert json.loads(selectors.to_json()) == stored(VALID)


def test_parse_selectors_reads_a_json_string() -> None:
    selectors = parse_selectors(json.dumps(VALID))

    assert selectors.list.title == "h2.listing-company a"


def test_unparsable_response_is_classified() -> None:
    with pytest.raises(SelectorSchemaError) as caught:
        parse_selectors("여기 있습니다: {list: ...")

    assert caught.value.reason == "unparsable"


def test_non_object_response_is_unparsable() -> None:
    with pytest.raises(SelectorSchemaError) as caught:
        parse_selectors('["ol > li"]')

    assert caught.value.reason == "unparsable"


def test_non_string_selector_is_unparsable() -> None:
    payload = json.loads(json.dumps(VALID))
    payload["list"]["title"] = {"css": "h2 a"}

    with pytest.raises(SelectorSchemaError) as caught:
        validate_selectors(payload)

    assert caught.value.reason == "unparsable"


def test_missing_section_is_classified() -> None:
    payload = {"list": VALID["list"]}

    with pytest.raises(SelectorSchemaError) as caught:
        validate_selectors(payload)

    assert caught.value.reason == "missing_field"
    assert "detail" in str(caught.value)


def test_missing_list_field_is_classified() -> None:
    payload = json.loads(json.dumps(VALID))
    del payload["list"]["date"]

    with pytest.raises(SelectorSchemaError) as caught:
        validate_selectors(payload)

    assert caught.value.reason == "missing_field"
    assert "date" in str(caught.value)


def test_empty_required_selector_is_missing() -> None:
    """값이 빈 문자열인 필수 필드는 있는 것이 아니라 없는 것이다."""
    payload = json.loads(json.dumps(VALID))
    payload["list"]["title"] = "   "

    with pytest.raises(SelectorSchemaError) as caught:
        validate_selectors(payload)

    assert caught.value.reason == "missing_field"
    assert "title" in str(caught.value)


def test_an_empty_link_passes_the_schema() -> None:
    """상세로 가는 a 가 없는 사이트에서 모델이 아무 요소나 대신 고르지 않게 한다.

    스키마를 통과한다는 것이 링크를 뽑을 수 있다는 뜻은 아니다. 그 판정은 자체 검증이 한다
    (`tests/test_selector_link.py`).
    """
    payload = json.loads(json.dumps(VALID))
    payload["list"]["link"] = ""

    selectors = validate_selectors(payload)

    assert selectors.list.link == ""
    assert selectors.list.link_template == ""


def test_unknown_field_is_classified_not_repaired() -> None:
    """`links` 를 `link` 로 추측해서 고치지 않는다."""
    payload = json.loads(json.dumps(VALID))
    payload["list"]["links"] = "a"

    with pytest.raises(SelectorSchemaError) as caught:
        validate_selectors(payload)

    assert caught.value.reason == "unknown_field"
    assert "links" in str(caught.value)


def test_unknown_section_is_classified() -> None:
    payload = json.loads(json.dumps(VALID))
    payload["pagination"] = {"next": "a.next"}

    with pytest.raises(SelectorSchemaError) as caught:
        validate_selectors(payload)

    assert caught.value.reason == "unknown_field"
    assert "pagination" in str(caught.value)


def test_optional_detail_fields_may_be_empty_but_must_exist() -> None:
    payload = json.loads(json.dumps(VALID))
    del payload["detail"]["department"]

    with pytest.raises(SelectorSchemaError) as caught:
        validate_selectors(payload)

    assert caught.value.reason == "missing_field"


def test_required_detail_field_may_not_be_empty() -> None:
    payload = json.loads(json.dumps(VALID))
    payload["detail"]["body"] = ""

    with pytest.raises(SelectorSchemaError) as caught:
        validate_selectors(payload)

    assert caught.value.reason == "missing_field"
    assert "body" in str(caught.value)
