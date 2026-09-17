"""본문 분류 테스트 (1.4.V).

Gemini 를 실제로 부르지 않는다. 응답은 가짜 클라이언트가 돌려주고, 확인하는 것은 넷이다 —
본문에 있는 값이 제 칸에 들어가는가, 본문에 없는 것이 빈 칸으로 남는가, **모델이 지어낸 값이
버려지는가**, 그리고 **판정 칸이 글자 일치 없이도 채워지는가.**

칸이 두 가지다. 뽑는 칸은 본문 글자를 그대로 옮기고, 판정 칸은 닫힌 목록에서 고른 뒤 근거
문장을 함께 낸다 (`app/classify/schema.py`).

본문은 저장된 픽스처에서 그 사이트의 설정으로 뽑는다. 손으로 지어낸 본문에 돌리면 실제
사이트의 글머리표와 줄바꿈을 못 보고 지나간다. 실사이트에 나가지 않는다.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from app.classify.classifier import (
    MAX_BODY_CHARS,
    ClassifyError,
    build_prompt,
    classify_body,
)
from app.classify.grounding import (
    NO_EVIDENCE,
    NOT_A_NUMBER,
    NOT_IN_LIST,
    NOT_IN_SOURCE,
    drop_exact_repeat,
    ground,
    in_body,
)
from app.classify.schema import (
    CLASSIFY_FIELDS,
    EXTRACT_FIELDS,
    JUDGE_CHOICES,
    JUDGE_FIELDS,
    NUMBER_FIELDS,
    VALUE_LABELS,
)
from app.config import Settings
from app.crawler.parser import parse_detail
from app.selector.schema import validate_selectors
from tests.classify_fakes import response
from tests.test_selector_generator import FakeClient

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SEEDS = pathlib.Path(__file__).parent.parent / "seeds" / "site-configs-20260826.json"

CONFIGS = {
    entry["name"]: entry for entry in json.loads(SEEDS.read_text(encoding="utf-8"))["crawlers"]
}


def body_of(site: str, fixture: str) -> str:
    """그 사이트의 셀렉터로 픽스처에서 뽑은 본문. 실행이 `raw_jobs` 에 싣는 것과 같다."""
    selectors = validate_selectors(CONFIGS[site]["selectors"]).detail
    html = (FIXTURES / fixture).read_text(encoding="utf-8")
    return parse_detail(html, selectors).fields["body"]


BODY = body_of("카카오", "kakao-detail-P-14503-20260826.html")
TITLE = "카카오비즈니스 파트너 플랫폼 PM (경력)"

# 제목이 말하는 직무를 본문도 되풀이하는 사이트. 열한 곳 중 셋뿐이고 두산이 그 하나다
# (`tests/test_job_role_source.py`)
DOOSAN_BODY = body_of("두산", "doosan-detail-1000361539-20260826.html")
DOOSAN_TITLE = "스튜디오셀위팀 광고영업 경력사원 채용"


def settings_with_key() -> Settings:
    return Settings(gemini_api_key="테스트키", gemini_model="gemini-3.5-flash")


async def classify(*texts: str, body: str = BODY, title: str = TITLE) -> tuple:
    client = FakeClient(*texts)
    result = await classify_body(body, title=title, settings=settings_with_key(), client=client)
    return result, client


def test_the_fixture_body_is_a_real_posting() -> None:
    """본문을 못 뽑으면 아래 테스트가 전부 빈 문자열을 상대로 웃으며 지나간다."""
    assert "◆ 지원자격" in BODY
    assert len(BODY) > 500


async def test_the_values_the_body_carries_land_in_their_columns() -> None:
    result, _ = await classify(
        response(
            qualifications="API 연동 아키텍처, 웹/앱 서비스의 데이터 흐름, 시스템 연동에 대한 "
            "기술적 이해도가 높으신 분",
            hiring_process="서류전형 > 1차 인터뷰 > 2차 인터뷰 > 처우 협의 > 최종 합격 및 입사",
        )
    )

    assert "API 연동 아키텍처" in result.postings[0].fields["qualifications"]
    assert result.postings[0].fields["hiring_process"].startswith("서류전형")
    assert result.postings[0].dropped == []
    assert result.attempts == 1


async def test_the_columns_the_body_does_not_name_stay_empty() -> None:
    """본문에 없는 것은 빈 칸이다. 이것이 이 작업의 전제다."""
    result, _ = await classify(
        response(
            hiring_process="서류전형 > 1차 인터뷰 > 2차 인터뷰 > 처우 협의 > 최종 합격 및 입사"
        )
    )

    assert result.postings[0].filled == ["hiring_process"]
    for name in CLASSIFY_FIELDS:
        if name != "hiring_process":
            assert result.postings[0].fields[name] == "", name


async def test_a_value_that_is_not_in_the_body_is_thrown_away() -> None:
    """일부러 본문에 없는 것을 답하게 한다. 그럴듯해도 버려야 한다."""
    result, _ = await classify(
        response(
            region="서울 강남구 테헤란로 123",
            hiring_process="서류전형 > 1차 인터뷰 > 2차 인터뷰 > 처우 협의 > 최종 합격 및 입사",
        )
    )

    assert result.postings[0].dropped == ["region"]
    assert result.postings[0].reasons["region"] == NOT_IN_SOURCE
    assert result.postings[0].fields["region"] == ""
    # 본문에 있는 값은 그대로 남는다. 한 칸이 틀렸다고 나머지를 버리지 않는다
    assert result.postings[0].fields["hiring_process"].startswith("서류전형")
    assert "버린 칸" in " ".join(result.notes)


async def test_only_the_invented_line_is_left_out_of_a_column() -> None:
    """한 줄이 원문에 없다고 제대로 옮긴 줄까지 버리지 않는다 (2026-09-10).

    조각이 가리키는 줄이 없으면(`tests/classify_fakes.py` 의 `NOWHERE`) 원문 어디에도 없는
    줄은 옮길 것이 없어 그 줄만 빠진다.
    """
    result, _ = await classify(
        response(
            preferred_qualifications=(
                "POS(포스), 키오스크, 테이블오더 등 오프라인 로컬 솔루션\n영어 능통자"
            )
        )
    )

    assert result.postings[0].dropped == []
    assert "키오스크" in result.postings[0].fields["preferred_qualifications"]
    assert "영어 능통자" not in result.postings[0].fields["preferred_qualifications"]
    assert any("찾지 못한 조각" in note for note in result.notes)


async def test_a_reflowed_quote_still_counts_as_being_in_the_body() -> None:
    """줄바꿈과 글머리표가 달라진 것을 지어냈다고 하면 멀쩡한 값이 버려진다.

    저장하는 글자는 모델이 적은 `-` 가 아니라 원문 글자다 (`app/classify/pieces.py`).
    """
    result, _ = await classify(
        response(
            responsibilities=(
                "- 카카오비즈니스와 외부 제휴사 간 사업자 데이터 연동 구조 기획 및 설계"
            )
        )
    )

    assert result.postings[0].dropped == []
    assert result.postings[0].fields["responsibilities"].startswith("카카오비즈니스")


async def test_a_column_the_schema_does_not_have_is_refused() -> None:
    client = FakeClient(json.dumps({"salary": "협의"}))

    with pytest.raises(ClassifyError) as caught:
        await classify_body(BODY, settings=settings_with_key(), client=client)

    assert caught.value.reason == "unknown_field"
    # 한 번 더 묻는다. DeepSeek 는 다시 물으면 대개 맞게 온다 (2026-09-17)
    assert len(client.calls) == 2


async def test_a_broken_response_is_asked_once_more() -> None:
    result, client = await classify(
        "{ 이건 JSON 이 아니다",
        response(
            hiring_process="서류전형 > 1차 인터뷰 > 2차 인터뷰 > 처우 협의 > 최종 합격 및 입사"
        ),
    )

    assert result.attempts == 2
    assert result.postings[0].fields["hiring_process"].startswith("서류전형")
    assert len(client.calls) == 2


async def test_it_gives_up_after_the_second_broken_response() -> None:
    client = FakeClient("깨진 응답", "여전히 깨진 응답")

    with pytest.raises(ClassifyError) as caught:
        await classify_body(BODY, settings=settings_with_key(), client=client)

    assert caught.value.reason == "unparsable"
    assert len(client.calls) == 2


async def test_an_empty_body_never_reaches_the_model() -> None:
    client = FakeClient(response())

    with pytest.raises(ClassifyError) as caught:
        await classify_body("   ", settings=settings_with_key(), client=client)

    assert caught.value.reason == "empty_body"
    assert client.calls == []


async def test_only_the_title_and_the_body_are_sent() -> None:
    """원본 HTML 도 페이지도 보내지 않는다 (`.claude/rules/llm.md`).

    제목이 하나 늘었다. `position_name` 의 출처라 보내지 않으면 그 칸이 영원히 빈다.
    """
    _, client = await classify(response())

    prompt = client.calls[0]["contents"]
    for line in BODY.splitlines():
        if line.strip():
            assert line.strip() in prompt
    assert TITLE in prompt
    assert "<html" not in prompt


async def test_the_job_role_lands_in_its_column() -> None:
    """제목이 말하는 직무가 그 칸에 들어간다 (2.3.V)."""
    result, _ = await classify(
        response(position_name="광고영업"), body=DOOSAN_BODY, title=DOOSAN_TITLE
    )

    assert result.postings[0].fields["position_name"] == "광고영업"
    assert result.postings[0].dropped == []


async def test_a_posting_whose_title_names_no_role_leaves_the_column_empty() -> None:
    """`전 직군 채용` 같은 통합 공고다. 짐작해서 채우면 소비 측이 그것을 사실로 노출한다."""
    result, _ = await classify(response(), title="토스인컴 전 직군 집중 채용 (~8/31)")

    assert result.postings[0].fields["position_name"] == ""
    assert "position_name" not in result.postings[0].filled
    assert result.postings[0].dropped == []


async def test_a_role_that_is_only_in_the_title_is_not_thrown_away() -> None:
    """열한 곳 중 여섯이 이 경우다. 본문에만 돌려 보면 맞게 뽑은 값이 통째로 버려진다 (2.4.V)."""
    # 이 값은 제목에 있고 본문에는 없다 (`tests/test_job_role_source.py`)
    assert not in_body("카카오비즈니스 파트너 플랫폼 PM", BODY)

    result, _ = await classify(response(position_name="카카오비즈니스 파트너 플랫폼 PM"))

    assert result.postings[0].fields["position_name"] == "카카오비즈니스 파트너 플랫폼 PM"
    assert result.postings[0].dropped == []


async def test_a_role_that_is_in_neither_the_title_nor_the_body_is_thrown_away() -> None:
    """제목을 더한 것이 검사를 끄는 것이 되면 안 된다 (2.4.V)."""
    result, _ = await classify(response(position_name="백엔드 개발자"))

    assert result.postings[0].dropped == ["position_name"]
    assert result.postings[0].reasons["position_name"] == NOT_IN_SOURCE
    assert result.postings[0].fields["position_name"] == ""


async def test_a_judgement_may_take_its_evidence_from_the_title() -> None:
    """`[채용연계형 인턴]` 은 고용형태의 근거다. 본문에 없다고 버릴 이유가 없다."""
    result, _ = await classify(
        response(employment_type="INTERN", employment_type_evidence="[채용연계형 인턴]"),
        title="[채용연계형 인턴] 파트너 영업 Specialist(신입)",
    )

    assert result.postings[0].fields["employment_type"] == "INTERN"
    assert result.postings[0].evidence["employment_type"] == "[채용연계형 인턴]"
    assert result.postings[0].dropped == []


def test_grounding_without_a_title_still_looks_at_the_body() -> None:
    """`title` 은 기본값이 있다. 주지 않으면 옛 동작 그대로다."""
    grounded = ground(
        {"responsibilities": "제휴사 데이터 연동 구조 기획"},
        "◆ 업무내용\n제휴사 데이터 연동 구조 기획",
    )

    assert grounded.dropped == []
    assert grounded.fields["responsibilities"] == "제휴사 데이터 연동 구조 기획"


async def test_a_posting_without_a_title_still_classifies() -> None:
    """제목이 없으면 직무만 빈다. 나머지 여덟 칸은 그대로 나온다."""
    result, _ = await classify(
        response(
            hiring_process="서류전형 > 1차 인터뷰 > 2차 인터뷰 > 처우 협의 > 최종 합격 및 입사"
        ),
        title="",
    )

    assert result.postings[0].fields["position_name"] == ""
    assert result.postings[0].fields["hiring_process"].startswith("서류전형")


def test_a_body_over_the_cap_is_cut_and_the_cut_is_written_down() -> None:
    prompt, notes = build_prompt("가" * (MAX_BODY_CHARS + 500))

    assert "가" * MAX_BODY_CHARS in prompt
    assert "가" * (MAX_BODY_CHARS + 1) not in prompt
    assert notes and str(MAX_BODY_CHARS) in notes[0]


async def test_the_call_reports_its_tokens_and_latency() -> None:
    """공고마다 붙는 호출이라 이 숫자가 없으면 비용 질문에 답할 수 없다."""
    result, _ = await classify(response())

    assert result.usage.model == "gemini-3.5-flash"
    assert result.usage.input_tokens == 4321
    assert result.usage.output_tokens == 120
    assert result.usage.latency_ms >= 0


def test_grounding_ignores_bullets_and_spacing_but_not_missing_words() -> None:
    body = "◆ 우대사항\n\n• SQL, Tableau 등 데이터 도구를 활용해 본 경험"

    assert in_body("- SQL, Tableau 등 데이터 도구를 활용해 본 경험", body)
    assert in_body("SQL Tableau 등 데이터 도구를 활용해 본 경험", body)
    assert not in_body("Python 을 활용해 본 경험", body)


def test_grounding_collapses_a_field_the_model_repeated_whole() -> None:
    """작고 빠른 모델이 한 칸 안에서 옮긴 문단을 통째로 두 번 낼 때가 있다(2026-08-29,
    토스뱅크 공고에서 관찰). 원문에는 한 번만 있는 문단이라 절반으로 접는다."""
    duty = "인프라 특화 AIOps 플랫폼을 설계·구축·운영해요."
    body = f"업무내용\n{duty}"

    grounded = ground({"responsibilities": f"{duty}\n{duty}"}, body)

    assert grounded.fields["responsibilities"] == duty
    assert grounded.dropped == []


def test_drop_exact_repeat_halves_an_even_repeated_block() -> None:
    assert drop_exact_repeat("가\n나\n가\n나") == "가\n나"


def test_drop_exact_repeat_leaves_an_odd_line_count_alone() -> None:
    assert drop_exact_repeat("가\n나\n다") == "가\n나\n다"


def test_drop_exact_repeat_leaves_a_mismatched_half_alone() -> None:
    assert drop_exact_repeat("가\n나\n다\n라") == "가\n나\n다\n라"


def test_grounding_leaves_a_genuine_two_line_value_alone() -> None:
    """줄이 둘이어도 서로 다르면 반복이 아니다 — 접지 않는다."""
    body = "자격요건\n경력 3년 이상\n영어 회화 가능"

    grounded = ground({"qualifications": "경력 3년 이상\n영어 회화 가능"}, body)

    assert grounded.fields["qualifications"] == "경력 3년 이상\n영어 회화 가능"


def test_grounding_keeps_an_empty_column_empty_without_calling_it_invented() -> None:
    grounded = ground({name: "" for name in CLASSIFY_FIELDS}, "본문")

    assert grounded.dropped == []
    assert set(grounded.fields) == set(CLASSIFY_FIELDS)


def test_the_three_kinds_of_columns_add_up() -> None:
    """칸이 늘거나 옮겨 다니면 여기서 걸린다."""
    assert set(EXTRACT_FIELDS) | set(JUDGE_FIELDS) | set(NUMBER_FIELDS) == set(CLASSIFY_FIELDS)
    assert not set(EXTRACT_FIELDS) & set(JUDGE_FIELDS)
    assert not set(NUMBER_FIELDS) & (set(EXTRACT_FIELDS) | set(JUDGE_FIELDS))


def test_the_judge_columns_have_a_closed_list() -> None:
    """목록을 정하지 않으면 같은 일이 사이트마다 다른 이름으로 쌓인다.

    목록은 오공고(Spring) enum 과 같다. 하나라도 갈리면 보낼 때 오공고가 400 으로 거절한다.
    """
    assert JUDGE_CHOICES["employment_type"] == (
        "FULL_TIME",
        "CONTRACT",
        "INTERN",
        "PART_TIME",
        "ETC",
    )
    assert JUDGE_CHOICES["experience_type"] == ("NEWCOMER", "EXPERIENCED", "BOTH", "IRRELEVANT")
    assert JUDGE_CHOICES["education_level"] == (
        "ANY",
        "HIGH_SCHOOL",
        "ASSOCIATE",
        "BACHELOR",
        "MASTER",
        "DOCTORATE",
    )
    assert JUDGE_CHOICES["closes_when_filled"] == ("true", "false")
    assert JUDGE_CHOICES["application_method"] == ("EXTERNAL_PAGE", "EMAIL")
    for values in JUDGE_CHOICES.values():
        assert "" not in values


async def test_a_judgement_does_not_need_the_words_to_be_in_the_body() -> None:
    """본문에 "경력" 이라고 적혀 있지 않다. 글자 일치를 요구하면 이 칸은 영원히 빈다."""
    result, _ = await classify(
        response(
            experience_type="EXPERIENCED",
            experience_type_evidence="Product Owner로서 5년 이상 경험이 있으신 분",
            employment_type="FULL_TIME",
            employment_type_evidence="정규직",
        )
    )

    assert result.postings[0].fields["experience_type"] == "EXPERIENCED"
    assert result.postings[0].fields["employment_type"] == "FULL_TIME"
    assert result.postings[0].dropped == []


async def test_a_judgement_whose_evidence_is_not_in_the_body_keeps_its_value() -> None:
    """판정 칸은 오공고가 반드시 받는다. 근거를 못 찾아도 AI 가 고른 값을 남긴다 (2026-09-14 결정).

    근거 문장은 남기지 않는다 — 검수 화면이 그 칸을 `근거 없음` 으로 보인다.
    """
    result, _ = await classify(
        response(
            experience_type="NEWCOMER",
            experience_type_evidence="신입 사원을 우대합니다",
        )
    )

    assert result.postings[0].dropped == []
    assert result.postings[0].fields["experience_type"] == "NEWCOMER"
    assert result.postings[0].evidence == {}


async def test_a_judgement_with_no_evidence_at_all_keeps_its_value() -> None:
    result, _ = await classify(response(employment_type="FULL_TIME"))

    assert result.postings[0].dropped == []
    assert result.postings[0].fields["employment_type"] == "FULL_TIME"
    assert "employment_type" not in result.postings[0].evidence


async def test_a_judgement_outside_the_list_is_thrown_away() -> None:
    """목록 밖 값이 한 번 들어오면 그 칸으로 거르는 소비 측이 조용히 그 건을 놓친다."""
    result, _ = await classify(
        response(employment_type="풀타임", employment_type_evidence="◆ 직원 유형")
    )

    assert result.postings[0].dropped == ["employment_type"]
    assert result.postings[0].reasons["employment_type"] == NOT_IN_LIST
    assert result.postings[0].fields["employment_type"] == ""


async def test_the_evidence_comes_back_with_the_result() -> None:
    """표본 스무 건 표가 판정 칸마다 근거 문장을 적어야 한다 (1.8.V)."""
    result, _ = await classify(
        response(employment_type="FULL_TIME", employment_type_evidence="◆ 직원 유형")
    )

    assert result.postings[0].evidence == {"employment_type": "◆ 직원 유형"}


async def test_the_prompt_carries_the_closed_list() -> None:
    """무엇 중에서 고르는지 모르는 채로 고르면 가장 가까운 값이 아니라 첫 값이 나온다."""
    _, client = await classify(response())

    prompt = client.calls[0]["contents"]
    for value in JUDGE_CHOICES["employment_type"]:
        assert value in prompt


async def test_the_response_schema_forces_the_list() -> None:
    """프롬프트로만 부탁하면 지킵니다가 아니라 대개 지킵니다가 된다."""
    _, client = await classify(response())

    from app.classify.schema import posting_model_of

    schema = client.calls[0]["config"]["response_schema"]
    assert posting_model_of(schema).model_fields["experience_type"].annotation is not str


def test_the_enum_never_carries_an_empty_value() -> None:
    """Gemini 가 빈 값이 든 enum 을 400 으로 거절한다 (2026-08-26 스무 건 표본에서 확인).

    그때 스무 건이 전부 `api_error` 로 끝났다. 스키마가 잘못되면 본문을 보내기도 전에
    막히므로 토큰은 나가지 않지만, 그 사실을 아는 데 실행 한 번이 든다.
    """
    from typing import get_args

    from app.classify.schema import Posting

    for name in JUDGE_FIELDS:
        values = get_args(Posting.model_fields[name].annotation)
        assert values, name
        assert "" not in values, name
        # 기본값이 없어 반드시 하나를 고른다. 화면 이름 표와도 같다
        assert Posting.model_fields[name].is_required(), name
        assert set(values) == set(VALUE_LABELS[name]), name


async def test_an_unanswered_judgement_is_blank_and_not_counted_as_invented() -> None:
    """스키마를 거치지 않은 응답이 판정 칸을 비워도 지어낸 것으로 세지 않는다."""
    result, _ = await classify(response(employment_type="", experience_type=""))

    assert result.postings[0].fields["employment_type"] == ""
    assert result.postings[0].dropped == []
    assert result.postings[0].evidence == {}


async def test_the_prompt_offers_no_undecided_answer() -> None:
    """판단불가 는 없앴다 (2026-09-14 결정).

    모델은 목록의 이름과 화면 이름을 함께 보고 하나를 고른다.
    """
    _, client = await classify(response())

    prompt = client.calls[0]["contents"]
    assert "판단불가" not in prompt
    assert "FULL_TIME(정규직)" in prompt


async def test_minimum_years_need_their_evidence_in_the_body() -> None:
    """최소 경력 연수는 사실 값이다. 근거 문장이 원문에 없으면 버린다."""
    kept, _ = await classify(
        response(
            experience_min_years="5",
            experience_min_years_evidence="Product Owner로서 5년 이상 경험이 있으신 분",
        )
    )
    invented, _ = await classify(response(experience_min_years="7"))

    assert kept.postings[0].fields["experience_min_years"] == "5"
    assert kept.postings[0].dropped == []
    assert invented.postings[0].fields["experience_min_years"] == ""
    assert invented.postings[0].reasons["experience_min_years"] == NO_EVIDENCE


async def test_minimum_years_that_are_not_a_number_are_thrown_away() -> None:
    result, _ = await classify(
        response(
            experience_min_years="5년",
            experience_min_years_evidence="Product Owner로서 5년 이상 경험이 있으신 분",
        )
    )

    assert result.postings[0].fields["experience_min_years"] == ""
    assert result.postings[0].reasons["experience_min_years"] == NOT_A_NUMBER


async def test_json_true_and_numbers_are_read_as_their_words() -> None:
    """스키마는 글자로 적게 하지만 모델이 JSON 참·숫자로 답해도 뜻은 같다. 다시 묻지 않는다."""
    result, _ = await classify(
        response(
            closes_when_filled=True,
            experience_min_years=5,
            experience_min_years_evidence="Product Owner로서 5년 이상 경험이 있으신 분",
        )
    )

    assert result.postings[0].fields["closes_when_filled"] == "true"
    assert result.postings[0].fields["experience_min_years"] == "5"
