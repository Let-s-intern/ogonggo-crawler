"""실패한 필드만 다시 고치게 하는 호출 (17.1).

2026-08-22 QA 의 롯데 등록을 픽스처로 재현한다. 모델이 계열사 링크 목록(`ul.family-group li`)
을 `list.item` 으로 잡아서, 항목은 4건 잡히는데 그 안에 제목도 링크도 날짜도 없다. 사람이
브라우저로 HTML 을 열어 `ul.job-card-list` 를 찾아 손으로 넣던 그 일이 대상이다.

Gemini 를 실제로 부르지 않는다. 응답은 전부 가짜 클라이언트가 돌려주고, 확인하는 것은
**무엇이 바뀌고 무엇이 그대로인가**다. Gemini 무료 한도가 분당 20회라 검증에서 실호출을
반복하지 않는다.
"""

from __future__ import annotations

import json
import logging
import pathlib
from typing import Any

import pytest

from app.config import Settings
from app.selector.repair import (
    SelectorRepairError,
    repair_from_html,
    repair_targets,
)
from app.selector.schema import validate_selectors
from app.selector.verify import verify_selectors
from tests.test_selector_generator import FakeClient

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
LIST_HTML = (FIXTURES / "wrong-item-list-20260822.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "wrong-item-detail-20260822.html").read_text(encoding="utf-8")

LIST_URL = "https://group.example.test/recruit"
DETAIL_URL = "https://group.example.test/recruit/view/2001"

# QA 에서 실제로 저장됐던 모양. 계열사 링크 목록을 항목으로 잡았다.
# 상세는 제대로 잡았고, 건너뜀 필드가 둘 있다
BROKEN: dict[str, Any] = {
    "list": {
        "item": "ul.family-group li",
        "title": "a.card-link",
        "link": "a.card-link",
        "date": "span.regdate",
        "company_name": "span.company",
        "link_template": "",
    },
    "detail": {
        "title": "h2.view-title",
        "body": "div.view-body",
        "qualifications": "",
        "recruitment_end_at": "dd.view-deadline",
        "department": "",
        "company_name": "",
    },
}


def response(**overrides: Any) -> str:
    """모델 응답 한 벌. 기본은 목록 필드를 제대로 고친 것이다."""
    payload = json.loads(json.dumps(BROKEN))
    payload["list"]["item"] = "ul.job-card-list > li.job-card"
    payload["list"].update(overrides.pop("list", {}))
    payload["detail"].update(overrides.pop("detail", {}))
    return json.dumps(payload, ensure_ascii=False)


def settings_with_key() -> Settings:
    return Settings(gemini_api_key="테스트키", gemini_model="gemini-3.5-flash")


async def repair(*texts: str, selectors: dict[str, Any] | None = None) -> Any:
    client = FakeClient(*(texts or (response(),)))
    outcome = await repair_from_html(
        LIST_HTML,
        DETAIL_HTML,
        validate_selectors(selectors or BROKEN),
        list_url=LIST_URL,
        detail_url=DETAIL_URL,
        settings=settings_with_key(),
        client=client,
    )
    return outcome, client


# 대상 고르기 -----------------------------------------------------------------


def test_only_failed_fields_become_targets() -> None:
    """건너뜀은 대상이 아니다. 사이트에 없다고 판정된 것을 억지로 만들지 않는다."""
    report = verify_selectors(validate_selectors(BROKEN), LIST_HTML, DETAIL_HTML)

    targets = repair_targets(report, has_detail_html=True)

    # 항목은 4건 잡히지만 그 안에 아무것도 없다. 그때는 항목 셀렉터도 대상이다
    assert report.list_fields_missing
    assert targets == [
        "list.item",
        "list.title",
        "list.link",
        "list.date",
        "list.company_name",
    ]
    assert "detail.qualifications" in report.skipped
    assert "detail.department" in report.skipped
    assert not set(targets) & set(report.skipped)


def test_detail_fields_are_not_targets_without_detail_html() -> None:
    """상세 URL 없이 등록한 크롤러. 0개 매칭이지만 볼 페이지가 없었을 뿐이다."""
    report = verify_selectors(validate_selectors(BROKEN), LIST_HTML, "")

    targets = repair_targets(report, has_detail_html=False)

    assert not [name for name in targets if name.startswith("detail.")]


async def test_nothing_to_repair_is_refused() -> None:
    """실패한 필드가 없으면 부르지 않는다. 잘 되는 셀렉터를 다시 만들지 않는다."""
    fixed = json.loads(response())
    client = FakeClient(response())

    with pytest.raises(SelectorRepairError) as caught:
        await repair_from_html(
            LIST_HTML,
            DETAIL_HTML,
            validate_selectors(fixed),
            settings=settings_with_key(),
            client=client,
        )

    assert caught.value.reason == "nothing_to_repair"
    # 고칠 것이 없으면 모델을 부르지도 않는다
    assert client.calls == []


# 고치기 ----------------------------------------------------------------------


async def test_failed_fields_change_and_working_fields_stay() -> None:
    outcome, _ = await repair()

    assert outcome.selectors.list.item == "ul.job-card-list > li.job-card"
    # 잘 되던 상세 필드는 그대로다
    assert outcome.selectors.detail.title == BROKEN["detail"]["title"]
    assert outcome.selectors.detail.body == BROKEN["detail"]["body"]
    assert outcome.selectors.detail.recruitment_end_at == BROKEN["detail"]["recruitment_end_at"]
    assert outcome.repaired == [
        "list.item",
        "list.title",
        "list.link",
        "list.date",
        "list.company_name",
    ]
    assert outcome.unresolved == []
    assert outcome.ok


async def test_a_model_answer_that_rewrites_a_working_field_is_discarded() -> None:
    """모델이 맞던 필드를 다른 값으로 내놔도 버린다. 프롬프트가 아니라 코드가 보장한다."""
    outcome, _ = await repair(
        response(detail={"title": "h1", "body": "body", "recruitment_end_at": "span"})
    )

    assert outcome.selectors.detail.title == "h2.view-title"
    assert outcome.selectors.detail.body == "div.view-body"
    assert outcome.selectors.detail.recruitment_end_at == "dd.view-deadline"
    assert [change.name for change in outcome.changes] == ["list.item"]


async def test_a_skipped_field_is_never_filled() -> None:
    """모델이 건너뜀 필드에 값을 채워 보내도 반영하지 않는다."""
    outcome, _ = await repair(
        response(detail={"qualifications": "div.view-body p", "department": "dd.view-dept"})
    )

    assert outcome.selectors.detail.qualifications == ""
    assert outcome.selectors.detail.department == ""


async def test_an_empty_answer_keeps_the_original_selector() -> None:
    """못 고쳤다는 응답이다. 빈 값으로 덮으면 손으로 고칠 대상조차 사라진다."""
    outcome, _ = await repair(response(list={"item": ""}))

    assert outcome.selectors.list.item == BROKEN["list"]["item"]
    assert outcome.changes == []
    assert outcome.unresolved == [
        "list.item",
        "list.title",
        "list.link",
        "list.date",
        "list.company_name",
    ]
    assert not outcome.ok


async def test_link_template_is_never_touched() -> None:
    """상세 URL 형식은 이 HTML 만으로 알 수 없다. 주소를 지어내게 두지 않는다."""
    outcome, _ = await repair(
        response(list={"link_template": "https://group.example.test/{data-id}"})
    )

    assert outcome.selectors.list.link_template == ""


# 프롬프트 --------------------------------------------------------------------


async def test_prompt_carries_current_selectors_and_failure_reasons() -> None:
    _, client = await repair()

    prompt = client.calls[0]["contents"]
    # 무엇이 이미 맞는지 알아야 그것을 피해 고른다
    assert "h2.view-title" in prompt
    # 실패한 필드와 사유
    assert "- list.title: 지금 `a.card-link`" in prompt
    assert "항목 4건 중 어디에도 없다" in prompt
    # 건너뜀 필드는 고칠 목록에 없다
    assert "- detail.qualifications" not in prompt


async def test_prompt_sends_cleaned_html_not_the_raw_page() -> None:
    client = FakeClient(response())
    noisy = LIST_HTML.replace("<main>", "<script>steal()</script><main>")

    await repair_from_html(
        noisy,
        DETAIL_HTML,
        validate_selectors(BROKEN),
        settings=settings_with_key(),
        client=client,
    )

    prompt = client.calls[0]["contents"]
    assert "steal()" not in prompt
    assert "job-card-list" in prompt


async def test_response_schema_is_the_same_as_generation() -> None:
    _, client = await repair()

    config = client.calls[0]["config"]
    assert config["response_mime_type"] == "application/json"
    assert config["response_schema"].__name__ == "SelectorSet"


# 재시도와 로그 ---------------------------------------------------------------


async def test_a_broken_response_is_retried_once() -> None:
    outcome, client = await repair("{ 이건 JSON 이 아니다", response())

    assert len(client.calls) == 2
    assert outcome.attempts == 2
    assert outcome.selectors.list.item == "ul.job-card-list > li.job-card"


async def test_two_broken_responses_stop_at_two_calls() -> None:
    client = FakeClient("깨짐", "여전히 깨짐", response())

    with pytest.raises(SelectorRepairError) as caught:
        await repair_from_html(
            LIST_HTML,
            DETAIL_HTML,
            validate_selectors(BROKEN),
            settings=settings_with_key(),
            client=client,
        )

    assert caught.value.reason == "unparsable"
    assert len(client.calls) == 2


async def test_usage_and_model_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="app.llm.gemini"):
        outcome, _ = await repair()

    assert outcome.usage.model == "gemini-3.5-flash"
    assert outcome.usage.input_tokens == 4321
    assert outcome.usage.output_tokens == 120
    assert outcome.usage.latency_ms >= 0
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "셀렉터 고치기 model=gemini-3.5-flash" in logged
    assert "input_tokens=4321" in logged
    assert "output_tokens=120" in logged
    assert "latency_ms=" in logged
