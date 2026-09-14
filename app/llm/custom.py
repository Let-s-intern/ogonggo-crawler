"""화면에서 추가한 OpenAI 호환 회사의 정의.

코드에 들어 있는 다섯(gemini·claude·gpt·qwen·ollama)은 회사마다 항목을 하나씩 쓴다. 여기 있는
것은 그 반대다 — **부르는 코드는 하나이고(`app/llm/openai_compat.py` 의 `custom_entry`), 회사마다
다른 점을 값으로 적는다.** DeepSeek·Kimi 처럼 Chat Completions 로 부르는 회사를 배포 없이 붙이려고
둔다 (2026-09-14 결정).

회사마다 다른 점은 실제로 부딪힌 것들이다.

- 답 형식을 강제하는 방법: `json_schema`(response_format), strict 도구 호출, 강제 없음(json_object)
- 요청에 더할 값: 생각 끄기 같은 회사 고유 값
- 온도: 고정해 두고 다른 값을 거절하는 모델이 있어 보낼지부터 정한다
- 응답 길이 상한: 기본값이 작아 긴 답이 잘리는 회사가 있다
- 이미지를 받는가, 모델 목록(`/models`)을 주는가

정의는 `app_settings` 의 `llm_custom_{이름}` 행에 JSON 으로 들어간다 (`app/llm/settings.py`).
단가는 저장만 한다. 쓰는 곳은 청구 예상 화면이다.
"""

from __future__ import annotations

import json
import re
from collections.abc import Container
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

JSON_SCHEMA = "json_schema"
STRICT_TOOL = "strict_tool"
NO_SCHEMA = "none"
SCHEMA_MODES: tuple[str, ...] = (JSON_SCHEMA, STRICT_TOOL, NO_SCHEMA)

# 화면에 보이는 이름
SCHEMA_MODE_LABELS: dict[str, str] = {
    JSON_SCHEMA: "json_schema (response_format)",
    STRICT_TOOL: "strict 도구 호출",
    NO_SCHEMA: "강제 없음 (json_object)",
}

# 이름은 `llm_calls.provider` 와 화면의 id·주소에 그대로 들어간다. 그래서 좁게 받는다
NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")


class CustomDefinitionError(ValueError):
    """쓸 수 없는 정의. 사유를 화면이 그대로 옮긴다."""


class ModelPrice(BaseModel):
    """모델 하나의 단가. 100만 토큰당 달러다."""

    model_config = ConfigDict(extra="forbid")

    input: float = Field(ge=0)
    output: float = Field(ge=0)


class CustomDefinition(BaseModel):
    """회사 하나의 정의. 키는 여기 없다 — 키는 다른 제공자와 같은 `llm_key_{이름}` 행이다."""

    model_config = ConfigDict(extra="forbid")

    label: str = ""
    base_url: str
    schema_mode: Literal["json_schema", "strict_tool", "none"]
    extra_body: dict[str, Any] = Field(default_factory=dict)
    # 비어 있으면 온도를 보내지 않는다. 부르는 쪽이 넘기는 온도는 쓰지 않는다
    temperature: float | None = Field(default=None, ge=0, le=2)
    # 비어 있으면 보내지 않고 회사 기본값을 따른다
    max_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    images: bool = False
    list_models: bool = True
    # 모델 목록(`/models`)을 받는 주소. 비어 있으면 호출 주소를 그대로 쓴다. strict 도구 호출을
    # 베타 주소에서만 받는 회사는 목록을 그 베타 주소에서 주지 않는다 (DeepSeek, 2026-09-14 확인)
    models_url: str = ""
    prices: dict[str, ModelPrice] = Field(default_factory=dict)

    @field_validator("base_url")
    @classmethod
    def _http_only(cls, value: str) -> str:
        trimmed = value.strip()
        if not trimmed.startswith(("http://", "https://")):
            raise ValueError("주소는 http:// 나 https:// 로 시작한다")
        return trimmed.rstrip("/")

    @field_validator("models_url")
    @classmethod
    def _http_or_blank(cls, value: str) -> str:
        trimmed = value.strip()
        if trimmed and not trimmed.startswith(("http://", "https://")):
            raise ValueError("모델 목록 주소는 비우거나 http:// 나 https:// 로 시작한다")
        return trimmed.rstrip("/")

    @field_validator("label")
    @classmethod
    def _trimmed(cls, value: str) -> str:
        return value.strip()


def check_name(name: str, reserved: Container[str]) -> str:
    """새 회사 이름. 코드에 들어 있는 제공자 이름과 겹치면 거절한다."""
    trimmed = name.strip()
    if not NAME_PATTERN.fullmatch(trimmed):
        raise CustomDefinitionError(
            f"이름 `{trimmed}` 을 쓸 수 없다. 영문 소문자로 시작하고 소문자·숫자·-·_ 로 2~32자다"
        )
    if trimmed in reserved:
        raise CustomDefinitionError(
            f"`{trimmed}` 은 코드에 들어 있는 제공자 이름이다. 다른 이름을 쓴다"
        )
    return trimmed


def parse(raw: str) -> CustomDefinition:
    """저장된 JSON 한 줄을 정의로 읽는다."""
    try:
        return CustomDefinition.model_validate_json(raw)
    except ValidationError as exc:
        raise CustomDefinitionError(_reasons(exc)) from exc


def from_form(
    *,
    label: str,
    base_url: str,
    schema_mode: str,
    temperature: str,
    max_tokens: str,
    images: bool,
    list_models: bool,
    extra_body: str,
    prices: str,
    models_url: str = "",
) -> CustomDefinition:
    """화면 폼의 글자들로 정의를 만든다. 빈 칸은 "보내지 않음" 이다."""
    if schema_mode not in SCHEMA_MODES:
        raise CustomDefinitionError(
            f"답 형식 강제 방식 `{schema_mode}` 을 모른다. {', '.join(SCHEMA_MODES)} 중 하나다"
        )
    values: dict[str, Any] = {
        "label": label,
        "base_url": base_url,
        "schema_mode": schema_mode,
        "temperature": _number(temperature, "온도", float),
        "max_tokens": _number(max_tokens, "응답 길이 상한", int),
        "images": images,
        "list_models": list_models,
        "models_url": models_url,
        "extra_body": _json_object(extra_body, "요청에 더할 값"),
        "prices": _json_object(prices, "모델별 단가"),
    }
    try:
        return CustomDefinition.model_validate(values)
    except ValidationError as exc:
        raise CustomDefinitionError(_reasons(exc)) from exc


def to_json(definition: CustomDefinition) -> str:
    return definition.model_dump_json()


def pretty(value: dict[str, Any]) -> str:
    """화면의 JSON 칸에 다시 채울 글자. 비어 있으면 빈 칸이다."""
    return json.dumps(value, ensure_ascii=False, indent=2) if value else ""


def _number(text: str, label: str, kind: type[float] | type[int]) -> float | int | None:
    trimmed = text.strip()
    if not trimmed:
        return None
    try:
        return kind(trimmed)
    except ValueError as exc:
        raise CustomDefinitionError(f"{label} `{trimmed}` 은 숫자가 아니다") from exc


def _json_object(text: str, label: str) -> dict[str, Any]:
    trimmed = text.strip()
    if not trimmed:
        return {}
    try:
        value = json.loads(trimmed)
    except json.JSONDecodeError as exc:
        raise CustomDefinitionError(f"{label} 을 JSON 으로 읽을 수 없다: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise CustomDefinitionError(f"{label} 은 {{ ... }} 모양의 JSON 객체여야 한다")
    return value


def _reasons(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error["loc"]) or "정의"
        parts.append(f"{where}: {error['msg']}")
    return "; ".join(parts)
