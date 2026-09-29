"""사이트에서 못 읽은 회사·모집 시작·모집 마감을 AI 가 짚은 값으로 채운다 (2026-09-17 결정).

APR 처럼 셀렉터가 제목·본문만 잡는 사이트는 세 칸이 늘 비었고, 마감이 비어 기간 채용이 상시 채용으로
나갔다. 분류는 줄 번호로 원문을 짚으니 세 칸도 짚고, 정규화는 사이트 값이 없을 때만 그 값을 쓴다.

| 확인 | 깨지면 |
|---|---|
| AI 는 세 칸을 줄 번호로 짚고 글자는 원문에서 온다 | AI 가 지어낸 회사·날짜가 들어간다 |
| 사이트에서 다 읽었으면 묻지 않는다 | 필요 없는 호출이 나간다 |
| 근거 문장 칸을 덧붙인 응답은 거절하지 않는다 | DeepSeek 분류가 자주 실패한다 |
| 사이트 값이 있으면 그 값이 먼저다 | 셀렉터로 잘 읽던 값이 AI 값으로 바뀐다 |
| 값이 없으면 AI 가 짚은 글자로 채우고 날짜로 읽는다 | 마감이 비어 상시 채용이 된다 |
| 연도 없는 날짜는 수집한 해, 반년 넘게 앞이면 다음 해다 | 지난해로 읽혀 마감으로 걸러진다 |
| 날짜를 못 찾으면 빈 값이고 실패하지 않는다 | 공고 하나가 통째로 정규화되지 않는다 |
| `D-32`·두 자리 연도·`18시` 도 읽는다 | 원문에 마감이 있는데 빈다 |
| 시각 없는 마감은 그날 23:59:59 다 | 마감 당일 00시에 닫힌다 |
| 모집 시작이 끝까지 비면 수집한 날이다 | 오공고가 시작 일시 없이 받는다 |
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.classify.basics import find_basics
from app.classify.schema import FALLBACK_FIELDS, STORED_CLASSIFY_FIELDS, validate_classification
from app.normalize import loose_date
from app.normalize.engine import apply_classification, fill_fallbacks
from tests.test_classify_body import settings_with_key
from tests.test_selector_generator import FakeClient

COLLECTED = date(2026, 9, 17)


BODY = (
    "함께하실 일본 마케팅팀을 소개합니다\n"
    "근무지: 잠실 롯데타워 36층, 에이피알 본사\n"
    "서류 제출 마감 기한은 9/20(일) 23: 59 입니다."
)
TITLE = "[26 신입채용] 일본 인플루언서 마케팅(PR)"


async def test_AI_는_세_칸을_줄_번호로_짚고_글자는_원문에서_온다() -> None:
    answer = {
        "company_name": [{"line": 2, "text": "에이피알"}],
        "recruitment_start_at": [],
        # 번호가 틀려도 원문에 있는 글자면 찾아 온다 (`app/classify/pieces.py`)
        "recruitment_end_at": [{"line": 1, "text": "9/20(일) 23: 59"}],
    }
    client = FakeClient(json.dumps(answer, ensure_ascii=False))

    values = await find_basics(
        BODY, TITLE, FALLBACK_FIELDS, settings=settings_with_key(), client=client
    )

    assert values == {
        "company_name": "에이피알",
        "recruitment_start_at": "",
        "recruitment_end_at": "9/20(일) 23: 59",
    }
    prompt = client.calls[0]["contents"]
    assert "[2] 근무지: 잠실 롯데타워 36층, 에이피알 본사" in prompt


async def test_지어낸_글자는_남지_않고_사이트에서_다_읽었으면_묻지_않는다() -> None:
    client = FakeClient(json.dumps({"company_name": [{"line": 99, "text": "삼성전자"}]}))

    values = await find_basics(
        BODY, TITLE, ["company_name"], settings=settings_with_key(), client=client
    )
    nothing = await find_basics(BODY, TITLE, [], settings=settings_with_key(), client=client)

    assert values == {"company_name": ""}
    assert nothing == {}
    assert len(client.calls) == 1


def test_근거_문장_칸을_덧붙인_응답은_거절하지_않는다() -> None:
    parsed = validate_classification(
        {"postings": [{"employment_type": "INTERN", "position_name_evidence": "제목"}]}
    )

    assert parsed.postings[0].fields["employment_type"] == "INTERN"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("9/20(일) 23: 59", "2026-09-20 23:59:00"),
        ("2026년 10월 3일", "2026-10-03 00:00:00"),
        ("2026.09.30 18:00 까지", "2026-09-30 18:00:00"),
        ("10월 5일(월)", "2026-10-05 00:00:00"),
        ("1/10(금)", "2027-01-10 00:00:00"),
        ("9/1(월) ~ 9/20(일)", "2026-09-01 00:00:00"),
        ("채용 시 마감", None),
        ("13/45", None),
        ("26.10.31(금)", "2026-10-31 00:00:00"),
        ("~ 26.10.31 23:59", "2026-10-31 23:59:00"),
        ("10월 31일(금) 18시", "2026-10-31 18:00:00"),
        ("2026.10.31 오후 6시 30분", "2026-10-31 18:30:00"),
        # 날짜가 함께 있으면 날짜가 먼저다
        ("~10/31(금) D-44", "2026-10-31 00:00:00"),
    ],
)
def test_짚은_글자에서_날짜를_읽는다(text: str, expected: str | None) -> None:
    assert loose_date.read(text, COLLECTED) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("10/31(금)", "2026-10-31 23:59:59"),
        ("10/31(금) 18:00", "2026-10-31 18:00:00"),
        ("D-32", "2026-10-19 23:59:59"),
        ("마감 D - 3", "2026-09-20 23:59:59"),
        ("D-day", "2026-09-17 23:59:59"),
        ("내일 마감", "2026-09-18 23:59:59"),
    ],
)
def test_시각_없는_마감은_그날_끝이다(text: str, expected: str) -> None:
    assert loose_date.read(text, COLLECTED, end=True) == expected


def test_시작일은_적힌_날짜만_읽고_없으면_수집한_날이다() -> None:
    assert loose_date.read("D-32", COLLECTED) is None
    fields: dict[str, str | None] = {
        "company_name": None,
        "recruitment_start_at": None,
        "recruitment_end_at": None,
    }
    written = classified(recruitment_start_at="26.09.01(월)", recruitment_end_at="D-32")
    fill_fallbacks(fields, written, COLLECTED)
    assert fields["recruitment_start_at"] == "2026-09-01 00:00:00"
    assert fields["recruitment_end_at"] == "2026-10-19 23:59:59"

    fields = {"company_name": None, "recruitment_start_at": None, "recruitment_end_at": None}
    fill_fallbacks(fields, classified(recruitment_start_at="D-32"), COLLECTED)
    assert fields["recruitment_start_at"] == "2026-09-17 00:00:00"


def classified(**values: str) -> dict[str, str]:
    return {name: "" for name in STORED_CLASSIFY_FIELDS} | values


def test_사이트_값이_있으면_그_값이_먼저다() -> None:
    fields: dict[str, str | None] = {
        "company_name": "에이피알",
        "recruitment_start_at": "2026-09-01 00:00:00",
        "recruitment_end_at": "2026-09-30 00:00:00",
    }
    ai = classified(company_name="다른회사", recruitment_start_at="9/2", recruitment_end_at="9/20")

    apply_classification(fields, ai)
    fill_fallbacks(fields, ai, COLLECTED)

    assert fields == {
        "company_name": "에이피알",
        "recruitment_start_at": "2026-09-01 00:00:00",
        "recruitment_end_at": "2026-09-30 00:00:00",
    } | {k: v for k, v in fields.items() if k not in FALLBACK_FIELDS}


def test_사이트_값이_없으면_AI_가_짚은_글자로_채운다() -> None:
    fields: dict[str, str | None] = {
        "company_name": None,
        "recruitment_start_at": None,
        "recruitment_end_at": None,
    }
    ai = classified(company_name="에이피알", recruitment_end_at="9/20(일) 23: 59")

    apply_classification(fields, ai)
    fill_fallbacks(fields, ai, COLLECTED)

    assert fields["company_name"] == "에이피알"
    assert fields["recruitment_end_at"] == "2026-09-20 23:59:00"
    # 모집 시작은 AI 도 못 찾아 수집한 날이다
    assert fields["recruitment_start_at"] == "2026-09-17 00:00:00"


def test_날짜를_못_읽으면_빈_값이고_분류가_없어도_시작은_수집한_날이다() -> None:
    fields: dict[str, str | None] = {"recruitment_start_at": None, "recruitment_end_at": None}

    fill_fallbacks(fields, classified(recruitment_end_at="채용 시 마감"), COLLECTED)
    assert fields["recruitment_end_at"] is None

    empty: dict[str, str | None] = {"recruitment_start_at": None, "recruitment_end_at": None}
    fill_fallbacks(empty, None, COLLECTED)
    assert empty["recruitment_start_at"] == "2026-09-17 00:00:00"
