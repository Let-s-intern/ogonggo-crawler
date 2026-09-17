"""셀렉터 JSON 스키마와 검증.

모델이 돌려준 것은 가설이지 결과가 아니다 (`.claude/rules/llm.md`). 이 파일이 그 가설을
DB 에 넣어도 되는 모양인지 판정한다.

판정은 통과 아니면 실패다. 스키마에 없는 필드명이 오면 무엇을 말하려던 것인지 추측해서 고치지
않는다 — 조용히 고친 셀렉터는 나중에 왜 안 맞는지 아무도 설명하지 못한다.

실패 사유는 셋 중 하나로 분류해서 운영자에게 그대로 보여준다.

| reason | 뜻 |
|---|---|
| `unparsable` | JSON 이 아니거나, 객체·문자열이 아닌 자리에 다른 타입이 왔다 |
| `unknown_field` | 스키마에 없는 필드명이 왔다 |
| `missing_field` | 필수 필드가 없거나 값이 비어 있다 |

`company_name`, `list.link_template`, 그리고 `SPLIT_DETAIL_FIELDS` 의 열 개는 키가 없어도, 빈
문자열이어도 통과한다 — 그래서 이 필드들이 생기기 전에 저장된 셀렉터 JSON 이 그대로 통과한다.
나머지 필드는 그대로 필수라, 값이 비어도 되는 상세 필드조차 키는 있어야 한다.

`unparsable` 만 1회 재생성 대상이다. 나머지는 모양이 아니라 내용의 문제라 다시 물어도 같은
답이 온다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

# 값이 비어 있어도 실패로 보지 않는 필드. 사이트에 그 항목 자체가 없을 수 있다.
# `link` 가 여기 있는 것은 링크가 없어도 된다는 뜻이 아니다. 상세로 가는 a 태그가 없는
# 사이트에서 모델이 아무 요소나 대신 고르지 않고 비워 두게 하려는 것이고, 비어 있으면
# 자체 검증이 `list.link` 를 실패로 적는다 (`app/selector/verify.py`).
#
# `date` 는 목록에 날짜를 안 적는 사이트가 있어서다. 네이버 목록은 모집 기간이 `dd.info_text`
# 다섯 개 중 하나로만 있어 모델이 두 번 다 비워 냈고, 그 빈 값 하나 때문에 테스트 실행이
# `invalid_selectors` 로 거절돼 크롤러를 아예 돌릴 수 없었다. 없는 것을 지어내는 것보다
# 비워 두는 편이 낫고, 마감일은 상세에서 온다
OPTIONAL_LIST_FIELDS: frozenset[str] = frozenset({"company_name", "link", "link_template", "date"})
# 0011 이 `normalized_jobs` 에 더한 열 칸을 상세에서 읽는 자리. 사이트가 그 값을 나눠서 줄
# 때만 채우고, 없으면 빈 문자열이다 — 없는 값을 다른 요소로 채우지 않는다
# (`migrations/0011_split_body_columns.sql`).
SPLIT_DETAIL_FIELDS: tuple[str, ...] = (
    "recruitment_start_at",
    "job_category",
    "employment_type",
    "experience_type",
    "region",
    "headcount",
    "responsibilities",
    "preferred_qualifications",
    "hiring_process",
    "recruitment_notice",
)

OPTIONAL_DETAIL_FIELDS: frozenset[str] = frozenset(
    {"qualifications", "recruitment_end_at", "department", "company_name", *SPLIT_DETAIL_FIELDS}
)
OPTIONAL_FIELDS: dict[str, frozenset[str]] = {
    "list": OPTIONAL_LIST_FIELDS,
    "detail": OPTIONAL_DETAIL_FIELDS,
}

# 키가 아예 없어도 되는 필드. 스키마에 나중에 더해진 것이라 그 전에 저장된 셀렉터 JSON 에는
# 키 자체가 없다. 나머지 필드는 값이 비어도 키는 있어야 한다.
OMITTABLE_FIELDS: frozenset[str] = frozenset(
    {"company_name", "link_template", *SPLIT_DETAIL_FIELDS}
)

# 아래 모델은 Gemini 의 response_schema 로 그대로 나간다. `extra="forbid"` 를 걸면
# `additionalProperties: false` 로 변환되는데 Gemini 가 그 필드를 모르고 400 을 낸다.
# 스키마에 없는 필드명을 거르는 일은 `validate_selectors()` 가 받은 뒤에 한다.


class ListSelectors(BaseModel):
    """목록 페이지. `item` 이 반복 단위고 나머지는 그 안에서 찾는다."""

    item: str
    title: str
    link: str
    date: str
    # 계열사 공고가 섞인 사이트에서 공고마다 다른 회사명을 잡는다. 없으면 빈 문자열이다
    company_name: str = ""
    # 상세 URL 을 속성값으로 만드는 사이트를 위한 것이다. 비어 있으면 지금까지처럼 `link` 가
    # 잡은 노드의 href 를 읽는다 — 방식을 적지 않은 기존 셀렉터가 그대로 동작한다.
    # 값이 있으면 `{속성이름}` 자리에 노드의 그 속성값을 끼워 URL 을 만든다. 자세한 것은
    # `app/selector/link.py` 에 있다
    link_template: str = ""


class DetailSelectors(BaseModel):
    """상세 페이지. 사이트에 없는 항목은 빈 문자열로 온다.

    `recruitment_start_at` 아래 열 개는 0011 이 `normalized_jobs` 에 더한 칸을 읽는 자리다. 전부
    기본값이 있어서, 이 필드들이 생기기 전에 저장된 셀렉터 JSON 이 키 없이도 그대로 통과한다.
    """

    title: str
    body: str
    qualifications: str
    recruitment_end_at: str
    department: str
    company_name: str = ""
    # 모집 마감일(`recruitment_end_at`)의 짝이다. 그 칸을 대신하지 않는다
    recruitment_start_at: str = ""
    job_category: str = ""
    employment_type: str = ""
    experience_type: str = ""
    region: str = ""
    headcount: str = ""
    responsibilities: str = ""
    preferred_qualifications: str = ""
    hiring_process: str = ""
    recruitment_notice: str = ""


class SelectorSet(BaseModel):
    """`crawlers.selectors_json` 에 그대로 들어가는 모양."""

    list: ListSelectors
    detail: DetailSelectors

    def to_json(self) -> str:
        return self.model_dump_json()


LIST_FIELDS: tuple[str, ...] = tuple(ListSelectors.model_fields)
# CSS 셀렉터인 목록 필드만. `link_template` 은 셀렉터가 아니라 URL 형식이라 HTML 에 돌리지
# 않는다
LIST_SELECTOR_FIELDS: tuple[str, ...] = tuple(
    name for name in LIST_FIELDS if name != "link_template"
)
DETAIL_FIELDS: tuple[str, ...] = tuple(DetailSelectors.model_fields)
SECTIONS: tuple[str, ...] = tuple(SelectorSet.model_fields)


class SelectorSchemaError(ValueError):
    """검증 실패. `reason` 은 위 표의 값 중 하나다."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def validate_selectors(data: Any) -> SelectorSet:
    """파싱된 응답을 검증한다. 통과하면 `SelectorSet`, 아니면 `SelectorSchemaError`."""
    return _build(data, allow_empty=False)[0]


def validate_selectors_allowing_empty(data: Any) -> tuple[SelectorSet, list[str]]:
    """필수 필드가 비어도 통과시키고, 비어 있던 필드 이름을 함께 돌려준다.

    재시도까지 해도 모델이 채우지 못한 응답을 위한 경로다. 통째로 버리면 운영자가 손으로
    고칠 대상조차 없다. 빈 채로 두고 이름을 알린다 — 무엇이었을지 추측해서 채우지 않는다
    (`.claude/rules/llm.md`).
    """
    return _build(data, allow_empty=True)


def _build(data: Any, *, allow_empty: bool) -> tuple[SelectorSet, list[str]]:
    if not isinstance(data, Mapping):
        raise SelectorSchemaError("unparsable", f"응답이 객체가 아니다: {type(data).__name__}")

    _reject_unknown(data, SECTIONS, "최상위")
    _require(data, SECTIONS, "최상위")

    sections: dict[str, dict[str, str]] = {}
    empty: list[str] = []
    for section, fields in (("list", LIST_FIELDS), ("detail", DETAIL_FIELDS)):
        sections[section], missing = _validate_section(
            data[section], section, fields, allow_empty=allow_empty
        )
        empty.extend(missing)

    return (
        SelectorSet(
            list=ListSelectors(**sections["list"]),
            detail=DetailSelectors(**sections["detail"]),
        ),
        empty,
    )


def parse_selectors(text: str) -> SelectorSet:
    """모델 응답 문자열을 파싱하고 검증한다."""
    return validate_selectors(_load(text))


def parse_selectors_allowing_empty(text: str) -> tuple[SelectorSet, list[str]]:
    """`validate_selectors_allowing_empty` 의 문자열 입력판."""
    return validate_selectors_allowing_empty(_load(text))


def parse_generated(text: str) -> tuple[SelectorSet, list[str]]:
    """모델이 낸 응답을 제자리로 옮긴 뒤 검증한다. 옮긴 것은 메모로 돌려준다.

    손으로 저장하는 셀렉터(`parse_selectors`)에는 쓰지 않는다. 사람이 적은 자리는 사람이 고친다.
    """
    data, notes = relocate_misplaced(_load(text))
    return validate_selectors(data), notes


def parse_generated_allowing_empty(text: str) -> tuple[SelectorSet, list[str], list[str]]:
    """`parse_generated` 의 빈 필드 허용판. (셀렉터, 빈 필드, 옮긴 메모).

    키를 아예 빼고 답한 칸도 빈 칸으로 받는다. 롯데ON 실측에서 모델이 상세의 `qualifications` 같은
    칸을 통째로 빼고 답했고, 키가 없다는 이유로 이 경로마저 예외를 던져 등록이 죽었다 (2026-09-17).
    """
    data, notes = relocate_misplaced(_load(text))
    selectors, empty = validate_selectors_allowing_empty(_with_missing_keys(data))
    return selectors, empty, notes


def _with_missing_keys(data: Any) -> Any:
    """두 묶음에 빠진 칸 이름을 빈 값으로 채운다. 묶음이 객체가 아니면 손대지 않는다."""
    if not isinstance(data, Mapping):
        return data
    filled = dict(data)
    for section, fields in (("list", LIST_FIELDS), ("detail", DETAIL_FIELDS)):
        value = filled.get(section, {})
        if not isinstance(value, Mapping):
            continue
        filled[section] = {**{name: "" for name in fields}, **value}
    return filled


def relocate_misplaced(data: Any) -> tuple[Any, list[str]]:
    """목록 칸을 상세에, 상세 칸을 목록에 넣은 응답을 제자리로 옮긴다 (2026-09-17).

    실제 등록 시험에서 DeepSeek 가 `link_template` 을 `detail` 안에 넣어 네이버·KT 등록이 통째로
    거절됐다. 스키마에 없는 이름을 지어낸 것이 아니라 있는 칸을 옆 묶음에 둔 것이라, 뜻을 추측하지
    않고 자리만 옮길 수 있다.

    옮기는 것은 반대쪽 묶음에만 있는 이름이다. 제자리에 이미 값이 있으면 제자리 값을 두고 잘못 놓인
    값은 버린다. 어느 묶음에도 없는 이름은 건드리지 않는다 — 검증이 그대로 거절한다.
    """
    if not isinstance(data, Mapping):
        return data, []
    moved = {section: dict(value) for section, value in data.items() if isinstance(value, Mapping)}
    if "list" not in moved or "detail" not in moved:
        return data, []
    notes: list[str] = []
    for source, target, fields in (
        ("detail", "list", LIST_FIELDS),
        ("list", "detail", DETAIL_FIELDS),
    ):
        own = LIST_FIELDS if source == "list" else DETAIL_FIELDS
        for name in [key for key in moved[source] if key not in own and key in fields]:
            value = moved[source].pop(name)
            current = moved[target].get(name)
            if isinstance(current, str) and current.strip():
                notes.append(f"`{source}.{name}` 을 버렸다 — `{target}.{name}` 에 이미 값이 있다")
                continue
            moved[target][name] = value
            notes.append(f"모델이 `{source}` 에 넣은 `{name}` 을 `{target}` 으로 옮겼다")
    return {**data, **moved}, notes


def _load(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise SelectorSchemaError("unparsable", f"JSON 으로 읽을 수 없다: {exc}") from exc


def _validate_section(
    value: Any, section: str, fields: tuple[str, ...], *, allow_empty: bool = False
) -> tuple[dict[str, str], list[str]]:
    if not isinstance(value, Mapping):
        raise SelectorSchemaError(
            "unparsable", f"`{section}` 이 객체가 아니다: {type(value).__name__}"
        )

    optional = OPTIONAL_FIELDS.get(section, frozenset())
    _reject_unknown(value, fields, section)
    _require(value, tuple(name for name in fields if name not in OMITTABLE_FIELDS), section)

    result: dict[str, str] = {}
    empty: list[str] = []
    for name in fields:
        # 나중에 더해진 필드는 키가 없어도 된다. 그 전에 저장된 셀렉터가 그렇다
        raw = value.get(name, "") if name in OMITTABLE_FIELDS else value[name]
        if not isinstance(raw, str):
            raise SelectorSchemaError(
                "unparsable", f"`{section}.{name}` 이 문자열이 아니다: {type(raw).__name__}"
            )
        selector = raw.strip()
        if not selector and name not in optional:
            if not allow_empty:
                raise SelectorSchemaError("missing_field", f"`{section}.{name}` 이 비어 있다")
            empty.append(f"{section}.{name}")
        result[name] = selector
    return result, empty


def _reject_unknown(data: Mapping[str, Any], allowed: tuple[str, ...], where: str) -> None:
    unknown = sorted(str(key) for key in data if key not in allowed)
    if unknown:
        raise SelectorSchemaError(
            "unknown_field", f"{where} 에 스키마에 없는 필드가 있다: {', '.join(unknown)}"
        )


def _require(data: Mapping[str, Any], required: tuple[str, ...], where: str) -> None:
    missing = [name for name in required if name not in data]
    if missing:
        raise SelectorSchemaError(
            "missing_field", f"{where} 에 필수 필드가 없다: {', '.join(missing)}"
        )
