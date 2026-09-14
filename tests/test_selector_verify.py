"""생성 시점 셀렉터 자체 검증 테스트.

저장된 픽스처에 셀렉터를 그대로 적용해 필드별 매칭 개수를 센다. Gemini 는 부르지 않는다 —
생성 경로 확인에는 가짜 클라이언트를 쓴다.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from app.selector.generator import generate_from_html
from app.selector.schema import validate_selectors
from app.selector.verify import verify_selectors
from tests.test_selector_generator import FakeClient, settings_with_key

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
LIST_HTML = (FIXTURES / "pythonorg-jobs-list-20260821.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "pythonorg-job-detail-20260821.html").read_text(encoding="utf-8")

# 2.3.V 에서 gemini-3.5-flash 가 이 픽스처로 실제로 생성한 셀렉터.
GENERATED: dict[str, Any] = {
    "list": {
        "item": "ol.list-recent-jobs > li",
        "title": "span.listing-company-name > a",
        "link": "span.listing-company-name > a",
        "date": "span.listing-posted time",
    },
    "detail": {
        "title": "h1.listing-company span.company-name",
        "body": "div.job-description",
        "qualifications": "",
        "recruitment_end_at": "",
        "department": "span.listing-company-category a",
    },
}


def selectors_with(**overrides: dict[str, str]) -> Any:
    payload = json.loads(json.dumps(GENERATED))
    for section, fields in overrides.items():
        payload[section].update(fields)
    return validate_selectors(payload)


def test_generated_selectors_match_the_saved_page() -> None:
    report = verify_selectors(selectors_with(), LIST_HTML, DETAIL_HTML)

    assert report.failed == []
    assert report.ok is True
    assert report.summary()["list.item"] > 1
    for name in ("list.title", "list.link", "list.date"):
        assert report.summary()[name] >= 1


def test_zero_match_field_is_named_not_swallowed() -> None:
    """일부러 틀린 셀렉터를 넣으면 그 필드 이름이 실패 목록에 뜬다."""
    report = verify_selectors(
        selectors_with(list={"date": "span.published-on"}), LIST_HTML, DETAIL_HTML
    )

    assert report.failed == ["list.date"]
    assert report.summary()["list.date"] == 0
    assert report.summary()["list.title"] >= 1


def test_broken_item_fails_every_list_field() -> None:
    report = verify_selectors(
        selectors_with(list={"item": "ul.job-cards > li"}), LIST_HTML, DETAIL_HTML
    )

    assert report.failed == ["list.item", "list.title", "list.link", "list.date"]


def test_detail_field_miss_is_named() -> None:
    report = verify_selectors(
        selectors_with(detail={"body": "div.posting-body"}), LIST_HTML, DETAIL_HTML
    )

    assert report.failed == ["detail.body"]


def test_empty_optional_selector_is_skipped_not_failed() -> None:
    """사이트에 그 항목이 없다는 응답이다. 0개 매칭과 같은 뜻이 아니다."""
    report = verify_selectors(selectors_with(), LIST_HTML, DETAIL_HTML)
    statuses = {field.name: field.status for field in report.fields}

    assert statuses["detail.qualifications"] == "skipped"
    assert statuses["detail.recruitment_end_at"] == "skipped"
    assert "detail.recruitment_end_at" not in report.failed


def test_syntax_error_is_a_failed_field_not_a_crash() -> None:
    report = verify_selectors(selectors_with(list={"title": "a[href"}), LIST_HTML, DETAIL_HTML)
    message = next(field.message for field in report.fields if field.name == "list.title")

    assert report.failed == ["list.title"]
    assert "문법" in message


async def test_generation_verifies_against_the_same_html() -> None:
    client = FakeClient(json.dumps(GENERATED))

    result = await generate_from_html(
        LIST_HTML, DETAIL_HTML, settings=settings_with_key(), client=client
    )

    assert result.ok is True
    assert result.verification.summary()["list.item"] > 1


async def test_generation_result_carries_the_failed_field_names() -> None:
    """0개 매칭 필드가 있어도 예외는 아니다. 실패한 필드 이름이 결과에 남는다."""
    payload = json.loads(json.dumps(GENERATED))
    payload["list"]["date"] = "span.published-on"
    client = FakeClient(json.dumps(payload))

    result = await generate_from_html(
        LIST_HTML, DETAIL_HTML, settings=settings_with_key(), client=client
    )

    assert result.ok is False
    assert result.verification.failed == ["list.date"]


@pytest.mark.parametrize("field_name", ["list.item", "list.title", "list.link", "list.date"])
def test_every_list_field_is_reported(field_name: str) -> None:
    report = verify_selectors(selectors_with(), LIST_HTML, DETAIL_HTML)

    assert field_name in report.summary()
