"""정규화 규칙 타입 검증 테스트.

확인하는 것은 하나다. 타입별로 정상 설정은 통과하고 잘못된 설정은 저장 단계에서 거부되는가.

거부는 사유까지 본다. 화면이 "어디를 고쳐야 하는지" 를 말하려면 `reason` 이 맞아야 한다.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.normalize.rules import (
    NORMALIZED_FIELDS,
    RULE_TYPES,
    DateParseConfig,
    MappingConfig,
    RegexConfig,
    RuleConfigError,
    TrimConfig,
    build_rule,
    parse_config,
)

VALID: list[tuple[str, dict[str, Any]]] = [
    ("mapping", {"map": {"영업직": "영업"}}),
    ("mapping", {"map": {"영업직": "영업"}, "default": "기타"}),
    ("regex", {"pattern": r"\s*\[광고\]\s*"}),
    ("regex", {"pattern": r"^모집분야\s*:\s*", "replacement": ""}),
    ("trim", {}),
    ("trim", {"collapse_whitespace": False, "strip_chars": "-·"}),
    ("date_parse", {"formats": ["%Y.%m.%d"]}),
    ("date_parse", {"formats": ["%Y.%m.%d", "%Y년 %m월 %d일"], "output_format": "%Y-%m-%d"}),
    # 두 자리 연도도 연도다
    ("date_parse", {"formats": ["%y.%m.%d"]}),
    # 리터럴 퍼센트 뒤에 붙은 연도 지시자는 지시자로 읽는다
    ("date_parse", {"formats": ["%%%Y-%m-%d"]}),
    ("html_text", {}),
]

INVALID: list[tuple[str, Any, str]] = [
    # 스키마에 없는 키. 오타가 조용히 무시되면 규칙이 안 먹는 이유를 못 찾는다
    ("mapping", {"map": {"a": "b"}, "fallback": "기타"}, "invalid_config"),
    ("trim", {"collapse": True}, "invalid_config"),
    ("html_text", {"keep_tags": ["b"]}, "invalid_config"),
    # 필수 키 누락
    ("mapping", {}, "invalid_config"),
    ("regex", {"replacement": ""}, "invalid_config"),
    ("date_parse", {"output_format": "%Y-%m-%d"}, "invalid_config"),
    # 값은 있는데 쓸 수 없다
    ("mapping", {"map": {}}, "invalid_config"),
    ("regex", {"pattern": "([unclosed"}, "invalid_config"),
    ("date_parse", {"formats": []}, "invalid_config"),
    ("date_parse", {"formats": ["%Y-%m-%d"], "output_format": ""}, "invalid_config"),
    # 연도가 없는 형식. `strptime` 이 1900 으로 채우므로 읽기에 **성공한 채로** 늘 오늘보다
    # 이전이 되고, 그 공고는 마감으로 읽혀 상세를 열지 않는다. 실패가 아니라 건너뜀이라
    # 실행은 성공으로 닫히고 그 사이트가 조용히 통째로 걸러진다
    # (`migrations/0027_drop_yearless_date_formats.sql`)
    ("date_parse", {"formats": ["%m/%d"]}, "invalid_config"),
    ("date_parse", {"formats": ["%m.%d"]}, "invalid_config"),
    # 하나라도 섞여 있으면 거부한다. 앞 형식이 잡아 주기를 기대할 수 없다
    ("date_parse", {"formats": ["%Y-%m-%d", "%m.%d"]}, "invalid_config"),
    # `%%` 는 리터럴 퍼센트라 연도 지시자가 아니다
    ("date_parse", {"formats": ["%%Y"]}, "invalid_config"),
    # 타입 자체가 틀렸다
    ("upper", {}, "unknown_type"),
    ("mapping", {"map": "영업직"}, "invalid_config"),
    ("mapping", "[]", "invalid_config"),
    ("mapping", "{not json}", "invalid_config"),
]


@pytest.mark.parametrize(("rule_type", "config"), VALID)
def test_valid_config_passes(rule_type: str, config: dict[str, Any]) -> None:
    parsed = parse_config(rule_type, config)
    # 설정은 저장할 수 있는 JSON 으로 다시 나와야 한다
    assert parsed.model_dump_json()


@pytest.mark.parametrize(("rule_type", "config", "reason"), INVALID)
def test_invalid_config_rejected(rule_type: str, config: Any, reason: str) -> None:
    with pytest.raises(RuleConfigError) as caught:
        parse_config(rule_type, config)
    assert caught.value.reason == reason


def test_every_rule_type_has_a_config() -> None:
    """모든 타입을 읽을 수 있어야 한다. 타입을 늘리고 스키마를 빠뜨리는 것을 막는다."""
    covered = {rule_type for rule_type, _ in VALID}
    assert covered == set(RULE_TYPES)


def test_config_json_round_trips() -> None:
    """저장한 문자열을 다시 읽어도 같은 설정이어야 한다."""
    rule = build_rule("region", "mapping", {"map": {"판교사옥": "판교"}, "default": "기타"})
    assert parse_config("mapping", rule.config_json()) == rule.config


def test_defaults_are_filled() -> None:
    assert parse_config("regex", {"pattern": "x"}) == RegexConfig(pattern="x", replacement="")
    assert parse_config("trim", {}) == TrimConfig(collapse_whitespace=True, strip_chars=None)
    assert parse_config("date_parse", {"formats": ["%Y"]}) == DateParseConfig(
        formats=["%Y"], output_format="%Y-%m-%d %H:%M:%S"
    )
    assert parse_config("mapping", {"map": {"a": "b"}}) == MappingConfig(map={"a": "b"})


@pytest.mark.parametrize("field_name", NORMALIZED_FIELDS)
def test_known_field_accepted(field_name: str) -> None:
    rule = build_rule(field_name, "trim", {})
    assert rule.field_name == field_name
    assert rule.enabled is True
    assert rule.priority == 0


@pytest.mark.parametrize("field_name", ["delivered_at", "source_url", "raw_data_json", "제목"])
def test_unknown_field_rejected(field_name: str) -> None:
    """`delivered_at` 은 제공 API 만 쓴다. 규칙이 손댈 수 있는 필드가 아니다."""
    with pytest.raises(RuleConfigError) as caught:
        build_rule(field_name, "trim", {})
    assert caught.value.reason == "unknown_field"


def test_invalid_rule_never_becomes_a_rule() -> None:
    """거부는 예외다. 반쯤 만들어진 Rule 이 돌아가지 않는다."""
    with pytest.raises(RuleConfigError):
        build_rule("title", "regex", {"pattern": "(["})
