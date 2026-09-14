"""정규화의 마무리와 칸 하나 정규화 (2026-09-14 결정).

오공고(Spring) `Job` 이 받는 모양으로 마무리한다 (`app/normalize/engine.py` 의 `settle_fields`).
실사이트에 나가지 않는다.

| 확인 | 깨지면 |
|---|---|
| 모집 인원은 처음 나오는 1 이상의 숫자다 | 오공고가 `3명` 이나 0 을 받지 않아 공고가 거절된다 |
| 가린 모집 인원(`0명`·`O명`·`00명`)은 비운다 | 0명 모집 공고로 나간다 |
| 최소 경력 연수는 경력 공고에만 남는다 | 신입 공고에 경력 연수가 붙는다 |
| 마감일이 있으면 기간 채용·자동 종료, 없으면 상시 채용이다 | 오공고가 거절한다 |
| 기간 앞쪽이 시작일이 되고, 수집한 시작일이 먼저다 | 시작일이 비거나 수집한 값이 덮인다 |
| 칸 하나 정규화는 그 칸의 규칙만 태운다 | 시작일 규칙 실패로 마감 거르기가 마감일을 못 읽는다 |
"""

from __future__ import annotations

import pytest

from app.normalize.engine import (
    NormalizeError,
    normalize_fields,
    normalize_value,
    settle_fields,
)
from app.normalize.rules import DERIVED_FIELDS, build_rule

PERIOD = build_rule(
    "recruitment_end_at", "regex", {"pattern": "^.*?[~〜]\\s*", "replacement": ""}, priority=0
)
DATES = {"formats": ["%Y-%m-%d %H:%M", "%Y.%m.%d"]}
RULES = [
    PERIOD,
    build_rule("recruitment_end_at", "date_parse", DATES, priority=10),
    build_rule("recruitment_start_at", "date_parse", DATES, priority=10),
]


def settled(**fields: str | None) -> dict[str, str | None]:
    return settle_fields(dict(fields))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3명", "3"),
        ("최대 2명", "2"),
        ("1,000명", "1000"),
        ("0명", None),
        ("O명", None),
        ("00명", None),
        ("약간명", None),
        (None, None),
    ],
)
def test_모집_인원은_처음_나오는_1_이상의_숫자다(text: str | None, expected: str | None) -> None:
    assert settled(recruitment_headcount=text)["recruitment_headcount"] == expected


def test_최소_경력_연수는_경력_공고에만_남는다() -> None:
    assert (
        settled(experience_type="EXPERIENCED", experience_min_years="03")["experience_min_years"]
        == "3"
    )
    for experience_type in ("NEWCOMER", "BOTH", "IRRELEVANT", None):
        fields = settled(experience_type=experience_type, experience_min_years="3")
        assert fields["experience_min_years"] is None, experience_type
    # 사람이 숫자가 아닌 글자로 고쳐도 그대로 내보내지 않는다
    assert (
        settled(experience_type="EXPERIENCED", experience_min_years="3년")["experience_min_years"]
        is None
    )


def test_마감일이_있으면_기간_채용이고_마감일에_닫힌다() -> None:
    fields = settled(recruitment_end_at="2026-09-30 23:59:59")

    assert (fields["recruitment_type"], fields["auto_close_enabled"]) == ("PERIOD", "true")


def test_마감일이_없으면_상시_채용이다() -> None:
    fields = settled(recruitment_end_at=None)

    assert (fields["recruitment_type"], fields["auto_close_enabled"]) == ("ALWAYS_OPEN", "false")
    assert set(DERIVED_FIELDS) <= set(fields)


def test_마감일_칸의_기간_앞쪽이_시작일이_된다() -> None:
    fields = normalize_fields({"recruitment_end_at": "2026-08-15 09:00 ~ 2026-08-30 17:00"}, RULES)

    assert fields["recruitment_start_at"] == "2026-08-15 09:00:00"
    assert fields["recruitment_end_at"] == "2026-08-30 17:00:00"


def test_다른_물결표도_기간으로_읽고_날짜만_있으면_하루의_시작과_끝이다() -> None:
    fields = normalize_fields({"recruitment_end_at": "2026.08.11 〜 2026.08.31"}, RULES)

    assert fields["recruitment_start_at"] == "2026-08-11 00:00:00"
    assert fields["recruitment_end_at"] == "2026-08-31 23:59:59"


def test_수집한_시작일이_있으면_기간으로_덮지_않는다() -> None:
    fields = normalize_fields(
        {
            "recruitment_end_at": "2026-08-15 09:00 ~ 2026-08-30 17:00",
            "recruitment_start_at": "2026.08.01",
        },
        RULES,
    )

    assert fields["recruitment_start_at"] == "2026-08-01 00:00:00"


def test_칸_하나는_그_칸의_규칙만_태운다() -> None:
    """시작일 규칙이 실패해도 마감 거르기는 마감일을 읽는다 (`app/crawler/deadline.py`)."""
    rules = [
        PERIOD,
        build_rule("recruitment_end_at", "date_parse", DATES, priority=10),
        build_rule("recruitment_start_at", "date_parse", {"formats": ["%Y/%m/%d"]}, priority=10),
    ]
    value = "2026.08.11 ~ 2026.08.31"

    assert normalize_value("recruitment_end_at", value, rules) == "2026-08-31 23:59:59"
    assert normalize_value("recruitment_end_at", "", rules) is None
    with pytest.raises(NormalizeError):
        normalize_fields({"recruitment_end_at": value}, rules)
