"""정규화 규칙의 타입과 설정 스키마.

규칙 하나는 "어떤 필드를(`field_name`) 어떤 방식으로(`rule_type`) 어떻게(`rule_config_json`)"
의 세 조각이다. 이 파일은 그 세 조각이 서로 맞는지만 판정하고, 값을 실제로 바꾸는 일은
`app/normalize/engine.py` 가 한다.

판정은 저장 전에 한다. `rule_config_json` 은 자유 형식 문자열이라 DB 의 CHECK 로는 막을 수
없고, 컴파일되지 않는 정규식이나 알 수 없는 키가 들어간 설정은 저장되는 순간 그 규칙을 쓰는
모든 실행이 같은 예외로 죽는다. 저장 단계에서 거부하는 편이 훨씬 싸다.

## 타입별 `rule_config_json` 스키마

| rule_type | 키 (필수는 굵게 표시하지 않고 아래 설명에 적는다) |
|---|---|
| `mapping` | `map` 필수, `default` 선택 |
| `regex` | `pattern` 필수, `replacement` 선택 |
| `trim` | `collapse_whitespace` 선택, `strip_chars` 선택 |
| `date_parse` | `formats` 필수, `output_format` 선택 |
| `html_text` | 없음. 빈 객체 |

`mapping` 은 값이 `map` 의 키와 정확히 같을 때 그 값으로 바꾼다. 표에 없으면 `default` 를 쓰고,
`default` 도 없으면 원문을 그대로 둔다.
`regex` 는 `re.sub(pattern, replacement, value)` 다.
`trim` 은 연속 공백을 하나로 접고 양끝을 깎는다.
`date_parse` 는 `formats` 를 적힌 순서대로 시도해 읽고 `output_format` 으로 다시 쓴다.
`html_text` 는 HTML 조각을 평문으로 편다. 설정이 없다 — 무엇을 줄바꿈으로 볼지는 값마다
고를 일이 아니라 HTML 이 정하는 것이고, 그 목록은 `app/crawler/parser.py` 에 하나만 있다.

키가 표에 없으면 거부한다. 오타 하나가 조용히 무시되면 규칙이 안 먹는 이유를 아무도 찾지 못한다.

실패 사유는 셋으로 나눈다. 화면이 어디를 고쳐야 하는지 그대로 말할 수 있어야 한다.

| reason | 뜻 |
|---|---|
| `unknown_type` | `rule_type` 이 네 가지 중 하나가 아니다 |
| `unknown_field` | `field_name` 이 `normalized_jobs` 에 없는 컬럼이다 |
| `invalid_config` | 타입은 맞는데 설정이 그 타입의 스키마에 맞지 않다 |
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

# 규칙이 값을 바꿀 수 있는 컬럼. `normalized_jobs` 에서 규칙이 만드는 것만 골라 적었다.
# `source_url`, `raw_job_id`, `normalized_at` 은 파이프라인이 채우고, `delivered_at` 은
# 제공 API 만 쓴다 (`.claude/rules/data-safety.md`).
#
# 뒤의 일곱은 0011 이 더한 칸이다. 사이트가 이미 나눠서 주는 값을 도로 합치지 않으려고
# 늘렸고, 넷 이상의 사이트가 주는 것만 골랐다
# (`migrations/0011_split_body_columns.sql`, `tests/test_split_body_columns.py`).
#
# 0016 이 `department`·`job_category`·`headcount` 를 뺐다. 값이 자리에 맞게 들어오지 않는
# 칸이었다 (`migrations/0016_drop_department_category_headcount.sql`). 0025 가 직무 분류 둘을
# 더했다 — `experience_type` 처럼 셀렉터가 채우는 칸이 아니라 분류가 `job_taxonomy` 표에서 골라
# 덮는 칸이다 (`migrations/0025_job_major_minor.sql`). 0028 이 오공고가 받는 다섯 칸을 더했다
# (`migrations/0028_add_posting_detail_fields.sql`).
#
# 0031 이 이름을 오공고(Spring) `Job` 엔티티의 칼럼 이름에 맞췄다. 제목에서 옮기던 자유 텍스트
# 직무는 받을 칸이 없어 뺐고, 직무 분류 둘이 `job_field`(직군)·`job_role`(직무)이 됐다
# (`migrations/0031_spring_field_names.sql`).
NORMALIZED_FIELDS: tuple[str, ...] = (
    "company_name",
    "title",
    "recruitment_end_at",
    "body",
    "qualifications",
    "recruitment_start_at",
    "employment_type",
    "experience_type",
    "region",
    "responsibilities",
    "preferred_qualifications",
    "hiring_process",
    "recruitment_notice",
    "job_field",
    "job_role",
    "company_and_team_introduction",
    "compensation",
    "benefits",
    "education_level",
    "recruitment_headcount",
)

# `normalization_rules.rule_type` 의 CHECK 제약과 같은 값이어야 한다.
RULE_TYPES: tuple[str, ...] = ("mapping", "regex", "trim", "date_parse", "html_text")

# `output_format` 이 실제로 렌더되는지 확인할 때만 쓰는 값. 어떤 날짜든 상관없다.
_FORMAT_PROBE = datetime(2000, 1, 2, 3, 4, 5)

# `date_parse` 의 `formats` 한 줄에 연도가 들어 있는지 본다. `%%` 는 리터럴 퍼센트라 지시자가
# 아니므로 먼저 걸러낸다 — `100%%Y` 같은 값이 연도를 가진 것으로 읽히면 안 된다.
#
# `%c` 와 `%x` 는 연도를 포함하지만 받지 않는다. 로케일이 정하는 형식이라 컨테이너의 로케일에
# 따라 같은 값이 다르게 읽히고, 그것은 여기서 막으려는 "조용히 틀린 날짜" 와 같은 종류다.
_YEAR_DIRECTIVE = re.compile(r"(?<!%)(?:%%)*%[Yy]")


class RuleConfigError(ValueError):
    """규칙을 저장할 수 없다. `reason` 은 위 표의 값 중 하나다."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class _Config(BaseModel):
    """모든 설정의 공통 규약. 스키마에 없는 키는 거부한다."""

    model_config = ConfigDict(extra="forbid")


class MappingConfig(_Config):
    """값 치환표. 회사명·고용형태처럼 사이트마다 표기가 갈리는 필드에 쓴다."""

    map: dict[str, str]
    # 표에 없는 값을 만났을 때. None 이면 원문을 그대로 둔다
    default: str | None = None

    @field_validator("map")
    @classmethod
    def not_empty(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError("map 이 비어 있으면 바꾸는 것이 없다")
        return value


class RegexConfig(_Config):
    """패턴 치환. 광고 문구 제거나 접두어 정리에 쓴다."""

    pattern: str
    replacement: str = ""

    @field_validator("pattern")
    @classmethod
    def compilable(cls, value: str) -> str:
        if not value:
            raise ValueError("pattern 이 비어 있다")
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"정규식으로 컴파일되지 않는다: {exc}") from exc
        return value


class TrimConfig(_Config):
    """공백 정리. 셀렉터가 제대로 잡은 값에 섞여 오는 개행·들여쓰기를 걷어낸다."""

    collapse_whitespace: bool = True
    # None 이면 파이썬 기본값(공백류 전체)으로 깎는다
    strip_chars: str | None = None


class DateParseConfig(_Config):
    """날짜 표기 통일. `formats` 는 시도할 순서대로 적는다."""

    formats: list[str]
    output_format: str = "%Y-%m-%d"

    @field_validator("formats")
    @classmethod
    def usable(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("formats 가 비어 있으면 읽을 방법이 없다")
        for item in value:
            if not item.strip():
                raise ValueError("formats 에 빈 문자열이 있다")
            if not _YEAR_DIRECTIVE.search(item):
                # 연도가 없으면 `strptime` 이 1900 으로 채운다. 그 값은 늘 오늘보다 이전이라
                # `app/crawler/deadline.py` 가 마감으로 읽고 그 공고의 상세를 열지 않는다.
                # 읽기에 실패한 것이 아니라 성공했는데 값이 틀린 것이라, 못 읽은 값을 진행
                # 중으로 두는 판정이 이 경우를 막지 못한다 — 그 사이트의 공고가 조용히 한
                # 건도 들어오지 않는다 (`migrations/0027_drop_yearless_date_formats.sql`).
                raise ValueError(
                    f"연도가 없는 형식은 받지 않는다: {item!r}. "
                    "%Y 나 %y 를 넣거나, 앞 순번의 regex 규칙으로 연도를 붙인 뒤 읽는다"
                )
        return value

    @field_validator("output_format")
    @classmethod
    def renderable(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("output_format 이 비어 있다")
        try:
            _FORMAT_PROBE.strftime(value)
        except ValueError as exc:
            raise ValueError(f"출력 형식으로 쓸 수 없다: {exc}") from exc
        return value


class HtmlTextConfig(_Config):
    """HTML 조각을 평문으로. 설정 값이 없다.

    `regex` 여러 개로 흉내내지 않는 이유는, 태그를 지우는 일과 줄을 바꾸는 일과 엔티티를
    되돌리는 일이 한 번에 일어나야 하기 때문이다. 순서를 하나 어긋나게 걸면 문장이 조용히
    붙어 버리고, 그 결과는 소비 측이 받은 뒤에야 보인다.
    """


RuleConfig = MappingConfig | RegexConfig | TrimConfig | DateParseConfig | HtmlTextConfig

_CONFIG_TYPES: dict[str, type[RuleConfig]] = {
    "mapping": MappingConfig,
    "regex": RegexConfig,
    "trim": TrimConfig,
    "date_parse": DateParseConfig,
    "html_text": HtmlTextConfig,
}


@dataclass(frozen=True)
class Rule:
    """`normalization_rules` 한 행. `config` 는 이미 검증된 상태다."""

    field_name: str
    rule_type: str
    config: RuleConfig
    priority: int = 0
    enabled: bool = True
    id: int | None = None

    def config_json(self) -> str:
        """`rule_config_json` 에 그대로 들어가는 문자열."""
        return self.config.model_dump_json()


def parse_config(rule_type: str, data: Any) -> RuleConfig:
    """타입에 맞는 설정으로 읽는다. 맞지 않으면 `RuleConfigError`.

    `data` 는 파싱된 객체이거나 JSON 문자열이다. 저장된 행에서 읽을 때는 후자다.
    """
    if rule_type not in _CONFIG_TYPES:
        raise RuleConfigError(
            "unknown_type",
            f"모르는 규칙 타입이다: {rule_type} (가능한 값: {', '.join(RULE_TYPES)})",
        )

    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise RuleConfigError(
                "invalid_config", f"설정을 JSON 으로 읽을 수 없다: {exc}"
            ) from exc

    if not isinstance(data, dict):
        raise RuleConfigError("invalid_config", f"설정이 객체가 아니다: {type(data).__name__}")

    try:
        return _CONFIG_TYPES[rule_type].model_validate(data)
    except ValidationError as exc:
        raise RuleConfigError("invalid_config", _explain(rule_type, exc)) from exc


def build_rule(
    field_name: str,
    rule_type: str,
    config: Any,
    *,
    priority: int = 0,
    enabled: bool = True,
    rule_id: int | None = None,
) -> Rule:
    """저장 직전의 검증. 통과한 것만 `normalization_rules` 에 들어간다."""
    if field_name not in NORMALIZED_FIELDS:
        raise RuleConfigError(
            "unknown_field",
            f"정규화할 수 없는 필드다: {field_name} (가능한 값: {', '.join(NORMALIZED_FIELDS)})",
        )
    return Rule(
        field_name=field_name,
        rule_type=rule_type,
        config=parse_config(rule_type, config),
        priority=priority,
        enabled=enabled,
        id=rule_id,
    )


def without_yearless_formats(rule_type: str, config: str) -> str:
    """`date_parse` 설정 문자열에서 연도가 없는 형식만 뺀다. 다른 규칙은 그대로 돌려준다.

    `migrations/0027_drop_yearless_date_formats.sql` 가 저장된 규칙에 한 일을, 그 마이그레이션을
    거치지 않고 들어오는 규칙(`app/api/import_data.py`)에 똑같이 한다. 형식이 하나도 남지 않으면
    빈 목록이 되고, 그 설정은 `DateParseConfig` 가 거절한다.

    JSON 은 공백 없이 다시 쓴다. 마이그레이션이 쓴 SQLite `json_set` 결과와 같은 모양이라,
    이미 정리된 같은 규칙과 글자 그대로 견줄 수 있다.
    """
    if rule_type != "date_parse":
        return config
    try:
        data = json.loads(config)
    except json.JSONDecodeError:
        return config
    formats = data.get("formats") if isinstance(data, dict) else None
    if not isinstance(formats, list):
        return config
    kept = [item for item in formats if not isinstance(item, str) or _YEAR_DIRECTIVE.search(item)]
    if len(kept) == len(formats):
        return config
    return json.dumps({**data, "formats": kept}, ensure_ascii=False, separators=(",", ":"))


def _explain(rule_type: str, exc: ValidationError) -> str:
    """pydantic 의 에러를 운영자가 읽을 한 줄로 옮긴다."""
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(item) for item in error["loc"]) or "(최상위)"
        parts.append(f"{location}: {error['msg']}")
    return f"{rule_type} 설정이 스키마에 맞지 않다 — {'; '.join(parts)}"
