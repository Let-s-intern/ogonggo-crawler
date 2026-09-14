"""값이 있는 칸을 원문과 견주는 제안 경로 (11.2.V ~ 11.4.V).

Gemini 를 실제로 부르지 않는다. 확인하는 것은 셋이다 — 이미 값이 있는 칸만 프롬프트에
"지금 값" 으로 나가는가, 같은 호출 한 번의 응답이 채우기(아홉 칸)와 제안(`company`·
`deadline`·`start_date`)으로 갈리는가, 그리고 제안에도 근거 검사가 그대로 걸리는가.

`job_field_suggestions` 표에 실제로 쓰는 것은 `app/classify/store.py` 의 함수를 보는
`tests/test_classify_suggestion_store.py` 다. 여기는 `classify_body` 한 겹만 본다.
"""

from __future__ import annotations

import pytest

from app.classify.classifier import build_prompt, classify_body
from app.classify.grounding import NOT_IN_SOURCE
from app.config import Settings
from tests.classify_fakes import response
from tests.test_selector_generator import FakeClient

BODY = (
    "◆ 업무내용\n제휴사 데이터 연동 구조 기획\n\n◆ 지원자격\n관련 경험 5년 이상이신 분\n"
    "◆ 접수 마감\n2026년 9월 30일까지\n◆ 모집 법인\n한화솔루션\n"
    "◆ 모집 시작일\n2026년 9월 1일부터\n"
)
TITLE = "마케팅 기획 경력직 채용"


def settings_with_key() -> Settings:
    return Settings(gemini_api_key="테스트키", gemini_model="gemini-3.5-flash")


GOOD = response(duties="제휴사 데이터 연동 구조 기획", requirements="관련 경험 5년 이상이신 분")


async def classify(*texts: str, current_values: dict[str, str] | None = None) -> tuple:
    client = FakeClient(*texts)
    result = await classify_body(
        BODY,
        title=TITLE,
        current_values=current_values,
        settings=settings_with_key(),
        client=client,
    )
    return result, client


def test_a_filled_field_appears_in_the_prompt() -> None:
    """값이 있는 칸만 "지금 값" 구역에 나간다 (11.2.V)."""
    prompt, _ = build_prompt(
        BODY, TITLE, current_values={"company": "한화생명", "deadline": "2026-08-31"}
    )

    assert "- 회사명 (company): 한화생명" in prompt
    assert "- 마감일 (deadline): 2026-08-31" in prompt
    assert "# 이미 있는 값 — 원문과 다르면 고쳐 제안한다" in prompt
    # 값을 주지 않은 셋째 칸은 나오지 않는다
    assert "(start_date)" not in prompt


def test_an_empty_current_values_produces_no_block() -> None:
    """빈 칸까지 나열하면 모델이 근거 없이 지어낼 자리가 생긴다 (11.2.V)."""
    prompt, _ = build_prompt(BODY, TITLE, current_values={})

    assert "# 이미 있는 값 — 원문과 다르면 고쳐 제안한다" not in prompt
    assert "(company)" not in prompt
    assert "(deadline)" not in prompt
    assert "(start_date)" not in prompt


def test_a_blank_field_is_left_out_even_when_others_are_filled() -> None:
    """셋 중 하나만 값이 있으면 그 하나만 프롬프트에 실린다 (11.2.V)."""
    prompt, _ = build_prompt(BODY, TITLE, current_values={"company": "한화생명"})

    assert "- 회사명 (company): 한화생명" in prompt
    assert "(deadline)" not in prompt
    assert "(start_date)" not in prompt


async def test_a_matching_suggestion_is_dropped() -> None:
    """모델이 지금 값과 같은 값을 돌려주면 바뀐 것이 없다. 제안이 아니다."""
    result, _ = await classify(
        response(company_suggestion="한화솔루션", company_suggestion_reason="원문과 같다"),
        current_values={"company": "한화솔루션"},
    )

    assert result.suggestions == {}


async def test_a_different_grounded_value_becomes_a_suggestion() -> None:
    """값이 있는 칸에 원문이 다른 값을 말하면 제안으로 나간다. 채우는 아홉 칸은 그대로다
    (11.3.V — 같은 호출 하나의 응답이 두 갈래로 갈리는지)."""
    result, _ = await classify(
        response(
            duties="제휴사 데이터 연동 구조 기획",
            deadline_suggestion="2026년 9월 30일까지",
            deadline_suggestion_reason="원문의 접수 마감이 다르다",
        ),
        current_values={"deadline": "2026-08-31"},
    )

    assert result.suggestions == {"deadline": "2026년 9월 30일까지"}
    assert result.suggestion_reasons["deadline"] == "원문의 접수 마감이 다르다"
    # 채우는 아홉 칸은 이 경로와 무관하게 그대로 채워진다
    assert result.postings[0].fields["duties"] == "제휴사 데이터 연동 구조 기획"
    # 제안 칸(company·start_date)은 값을 안 줬으니 비어 있다
    assert "company" not in result.suggestions


async def test_a_suggestion_without_evidence_in_the_source_is_thrown_away() -> None:
    """제안이라고 근거 검사를 느슨하게 하지 않는다 (11.4.V)."""
    result, _ = await classify(
        response(
            company_suggestion="완전히 다른 회사",
            company_suggestion_reason="지어낸 이유",
        ),
        current_values={"company": "한화솔루션"},
    )

    assert result.suggestions == {}
    assert result.postings[0].dropped == []  # 채우는 아홉 칸의 근거 검사와는 다른 목록이다


async def test_no_current_value_means_no_suggestion_even_if_the_model_answers() -> None:
    """지금 값이 없는 칸은 채우기 대상이지 제안 대상이 아니다. 셋 다 채우지 않는다 (11.2.V)."""
    result, _ = await classify(
        response(
            deadline_suggestion="2026년 9월 30일까지",
            deadline_suggestion_reason="원문에 마감이 있다",
        ),
        current_values={},
    )

    assert result.suggestions == {}


async def test_a_reflowed_suggestion_still_counts_as_grounded() -> None:
    """줄바꿈·공백이 다른 것을 지어냈다고 하면 멀쩡한 제안이 버려진다."""
    result, _ = await classify(
        response(
            deadline_suggestion="2026년 9월 30일까지  ",
            deadline_suggestion_reason="원문의 접수 마감",
        ),
        current_values={"deadline": "2026-08-31"},
    )

    assert result.suggestions["deadline"].strip() == "2026년 9월 30일까지"


@pytest.mark.parametrize(
    ("field_name", "current", "new_value"),
    [
        ("company", "한화생명", "한화솔루션"),
        ("deadline", "2026-08-31", "2026년 9월 30일까지"),
        ("start_date", "2026-08-01", "2026년 9월 1일부터"),
    ],
)
async def test_each_review_field_can_be_suggested(
    field_name: str, current: str, new_value: str
) -> None:
    """세 칸 모두 제안 경로를 탄다. 하나만 시험하면 나머지 둘의 배선 실수를 놓친다."""
    result, _ = await classify(
        response(
            **{
                f"{field_name}_suggestion": new_value,
                f"{field_name}_suggestion_reason": "원문과 다르다",
            }
        ),
        current_values={field_name: current},
    )

    assert result.suggestions == {field_name: new_value}


def test_only_the_three_review_fields_are_offered() -> None:
    """`title`·`body` 는 대상이 아니다 — title 은 이미 입력이고 body 는 원문 자체다."""
    prompt, _ = build_prompt(
        BODY,
        TITLE,
        current_values={"title": TITLE, "body": BODY, "company": "한화솔루션"},
    )

    # title·body 는 COLLECTED_REVIEW_FIELDS 밖이라 무시된다. 목록에 한 줄만 남는다
    assert prompt.count("# 이미 있는 값 — 원문과 다르면 고쳐 제안한다") == 1
    assert "- 회사명 (company): 한화솔루션" in prompt
    assert "(title)" not in prompt
    assert "(body)" not in prompt


async def test_a_bare_not_in_source_reason_is_reused_for_extract_fields() -> None:
    """제안과 무관한 회귀 확인 — 채우는 칸의 근거 검사 문구는 그대로다."""
    result, _ = await classify(response(work_location="원문에 없는 근무지"))

    assert result.postings[0].dropped == ["work_location"]
    assert result.postings[0].reasons["work_location"] == NOT_IN_SOURCE
