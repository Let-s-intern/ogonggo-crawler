"""OpenAI SDK 로 부르는 제공자들. GPT 와 Qwen 과 Ollama Cloud, 그리고 화면에서 추가한 회사다.

Qwen(DashScope)과 Ollama Cloud 가 OpenAI 호환 엔드포인트를 준다. `openai` SDK 에 `base_url`
만 바꿔 붙기 때문에 SDK 를 하나 더 들이지 않아도 된다
(`.claude/tasks/memos/llm-provider-조사.md`).

**호환은 같은 제공자라는 뜻이 아니다.** 항목은 따로다 — 키도 모델 ID 도 요금도 다르고,
`llm_calls.provider` 에 남아야 하는 이름도 다르다. 공유하는 것은 호출하는 코드뿐이다.

`.parse()` 에 Pydantic 클래스를 그대로 넘긴다. SDK 가 `strict` JSON 스키마로 바꿔 주므로
`app/classify/schema.py` 의 클래스를 제공자마다 고쳐 둘 필요가 없다. Gemini 가 싫어하는
`additionalProperties: false` 도 이 변환이 알아서 붙인다 — 그 차이가 이 파일 안에서 끝난다.

화면에서 추가한 회사는 코드에 항목이 없다. 부르는 함수 하나(`custom_entry`)가 회사 정의를 받아
항목을 만든다 — 답 형식을 강제하는 방법, 더할 값, 온도, 응답 길이 상한이 회사마다 달라서
그것을 값으로 받는다 (`app/llm/custom.py`).
"""

from __future__ import annotations

import base64
import json
import logging
import time
from collections.abc import Sequence
from typing import Any

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel

from app.config import Settings
from app.llm.base import ImageInput, LlmCallError, Provider, Usage, log_usage
from app.llm.custom import JSON_SCHEMA, NO_SCHEMA, STRICT_TOOL, CustomDefinition

logger = logging.getLogger(__name__)

GPT = "gpt"
QWEN = "qwen"
OLLAMA = "ollama"

# 응답을 스키마로 강제하는 Qwen 모델. 문서가 지원을 시리즈 단위로 적어서 앞자리로 맞춘다.
# **별칭(`qwen-turbo`, `qwen-plus`, `qwen-flash`)은 여기 없다.** 별칭에서 되는 것은
# `json_object` 뿐이고, 그것은 칸 이름도 값도 보장하지 않는다 — 분류에 쓸 수 없다
QWEN_SCHEMA_MODELS = (
    "qwen3.7-plus",
    "qwen3.7-flash",
    "qwen3.7-max",
    "qwen3.8-flash",
    "qwen3.8-max",
)

# strict 도구 호출에서 답을 받는 도구 이름
ANSWER_TOOL = "answer"

# strict 도구 스키마에서 떼는 주석 낱말. 값의 모양을 정하지 않고, strict 모드가 받지 않는
# 회사가 있다. **속성 이름은 떼지 않는다** — `title` 이라는 칸이 있는 스키마(셀렉터)가 있다
_ANNOTATIONS = frozenset({"title", "default"})
_NAME_MAPS = frozenset({"properties", "$defs", "definitions"})


def _build(key_setting: str, base_url_setting: str | None) -> Any:
    def build_client(settings: Settings) -> AsyncOpenAI:
        """API 키는 설정에서만 온다. 소스에도 로그에도 남기지 않는다.

        키를 반드시 넘긴다. 넘기지 않으면 SDK 가 환경의 `OPENAI_API_KEY` 를 스스로 읽어,
        이 서비스가 설정한 적 없는 키로 호출이 나간다.
        """
        api_key = getattr(settings, key_setting)
        if not api_key:
            raise LlmCallError("no_api_key", "API 키가 없다")
        # 주소를 갈아 끼우는 것은 호환 엔드포인트를 쓰는 제공자뿐이다. GPT 는 SDK 의 기본
        # 주소를 그대로 쓰므로 `None` 이고, 그때는 인자를 넘기지 않는다
        base_url = getattr(settings, base_url_setting) if base_url_setting else None
        return AsyncOpenAI(api_key=api_key, base_url=base_url)

    return build_client


def _content(prompt: str, images: Sequence[ImageInput]) -> Any:
    """사용자 메시지 본문. 이미지가 없으면 지금까지처럼 글자 하나다."""
    if not images:
        return prompt
    # 이미지를 데이터 주소로 앞에, 지시를 뒤에 둔다. 조각 순서가 곧 위에서 아래 순서다
    return [
        *(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image.mime_type};base64,"
                    + base64.b64encode(image.data).decode("ascii")
                },
            }
            for image in images
        ),
        {"type": "text", "text": prompt},
    ]


def _caller(name: str, label: str) -> Any:
    async def call_model(
        client: Any,
        model: str,
        prompt: str,
        attempt: int,
        kind: str,
        *,
        response_schema: Any,
        system_instruction: str,
        temperature: float = 0.0,
        images: Sequence[ImageInput] = (),
    ) -> tuple[str, Usage]:
        """호출 1회. 모델 ID·토큰·지연을 남긴다."""
        started = time.monotonic()
        try:
            response = await client.chat.completions.parse(
                model=model,
                messages=[
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": _content(prompt, images)},
                ],
                response_format=response_schema,
                temperature=temperature,
            )
        except openai.APIError as exc:
            # 운영자에게 보이는 문구에는 상태 코드와 메시지만 옮긴다. 키는 헤더로만 나간다.
            # 크레딧 소진에 전용 예외가 없어 429 로 오므로, 메시지를 남겨야 "한도인가
            # 잔액인가" 를 나중에 가를 수 있다
            code = getattr(exc, "status_code", "없음")
            raise LlmCallError("api_error", f"{label} 호출 실패({code}): {exc}") from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        usage = _usage(name, model, response, latency_ms)
        log_usage(logger, kind, attempt, usage, _finish_reason(response))
        return _text(response), usage

    return call_model


async def list_models(client: Any) -> list[str]:
    """지금 부를 수 있는 모델 ID. 셋 다 같은 모양이라 함수 하나로 끝난다."""
    page = await client.models.list()
    return [str(model.id) for model in getattr(page, "data", []) if getattr(model, "id", "")]


def entry(
    name: str,
    label: str,
    key_setting: str,
    model_setting: str,
    base_url_setting: str | None,
    schema_models: tuple[str, ...] | None,
) -> Provider:
    """OpenAI SDK 로 부르는 제공자 항목 하나."""
    return Provider(
        name=name,
        sdk="openai",
        key_setting=key_setting,
        model_setting=model_setting,
        build_client=_build(key_setting, base_url_setting),
        call_model=_caller(name, label),
        schema_models=schema_models,
        list_models=list_models,
    )


QWEN_PROVIDER = entry(
    name=QWEN,
    label="Qwen",
    key_setting="qwen_api_key",
    model_setting="qwen_model",
    base_url_setting="qwen_base_url",
    schema_models=QWEN_SCHEMA_MODELS,
)

# Structured Outputs 는 모델에 붙은 기능이 아니라 API 의 기능이라, `schema_models` 를 두지
# 않는다. 다만 지원 모델 표가 `gpt-4o-2024-08-06` 과 "그 이후" 까지만 적고 `gpt-5.6` 계열의
# 이름을 따로 적지는 않는다. 그보다 오래된 모델을 설정에 넣으면 이 가정이 깨진다
GPT_PROVIDER = entry(
    name=GPT,
    label="GPT",
    key_setting="gpt_api_key",
    model_setting="gpt_model",
    base_url_setting=None,
    schema_models=None,
)

# **빈 튜플은 "어느 모델도 강제하지 못한다" 는 뜻이다.** 문서가 "Ollama's Cloud currently
# does not support structured outputs" 라고 적는다 — 로컬 Ollama 는 `format` 으로 되지만
# 클라우드는 안 된다. 그래서 분류에는 쓸 수 없고 `no_schema_support` 로 거절된다.
# 셀렉터 생성과 AI 수정에는 쓸 수 있다. 그쪽은 스키마를 벗어난 응답이 와도
# `parse_selectors()` 가 거절하는 길이 있다 (`app/llm/providers.py` 의 `NEEDS_SCHEMA`)
OLLAMA_PROVIDER = entry(
    name=OLLAMA,
    label="Ollama Cloud",
    key_setting="ollama_api_key",
    model_setting="ollama_model",
    base_url_setting="ollama_base_url",
    schema_models=(),
)


def custom_entry(name: str, definition: CustomDefinition) -> Provider:
    """화면에서 추가한 회사 하나의 항목. 부르는 코드는 하나이고 회사마다 다른 점은 정의가 정한다.

    키와 모델은 설정 칸이 아니라 설정의 사전에서 읽는다 — 회사 이름이 정해져 있지 않아 칸을
    미리 만들 수 없다. 기능마다 그 사전을 채우는 것은 `app/llm/settings.py` 의 `settings_for` 다.
    """
    return Provider(
        name=name,
        sdk="openai",
        key_setting="",
        model_setting="",
        build_client=_custom_client(name, definition),
        call_model=_custom_caller(name, definition.label or name, definition),
        list_models=_custom_lister(definition) if definition.list_models else None,
        # 강제 없음은 어느 모델도 강제하지 못한다는 뜻이라 빈 튜플이다 (Ollama Cloud 와 같다)
        schema_models=() if definition.schema_mode == NO_SCHEMA else None,
        images=definition.images,
        read_key=lambda settings: settings.llm_custom_keys.get(name, ""),
        read_model=lambda settings: settings.llm_custom_models.get(name, ""),
    )


def _custom_client(name: str, definition: CustomDefinition) -> Any:
    def build_client(settings: Settings) -> AsyncOpenAI:
        """키는 설정에서만 온다. 반드시 넘긴다 — 넘기지 않으면 SDK 가 환경의 키를 읽는다."""
        api_key = settings.llm_custom_keys.get(name, "").strip()
        if not api_key:
            raise LlmCallError("no_api_key", "API 키가 없다")
        return AsyncOpenAI(api_key=api_key, base_url=definition.base_url)

    return build_client


def _custom_lister(definition: CustomDefinition) -> Any:
    async def list_custom_models(client: Any) -> list[str]:
        """모델 목록. 목록 주소가 따로 적혀 있으면 같은 키로 그 주소에 묻는다."""
        if definition.models_url:
            client = client.with_options(base_url=definition.models_url)
        return await list_models(client)

    return list_custom_models


def _custom_caller(name: str, label: str, definition: CustomDefinition) -> Any:
    async def call_model(
        client: Any,
        model: str,
        prompt: str,
        attempt: int,
        kind: str,
        *,
        response_schema: Any,
        system_instruction: str,
        temperature: float = 0.0,
        images: Sequence[ImageInput] = (),
    ) -> tuple[str, Usage]:
        """호출 1회. **부르는 쪽이 넘긴 온도는 쓰지 않는다.**

        온도를 보낼지와 그 값은 회사 정의가 정한다. 온도를 고정해 두고 다른 값을 보내면 거절하는
        모델이 있어서, 기능마다 0 을 넘기는 지금 모양을 그대로 보내면 호출이 선다.
        """
        if images and not definition.images:
            raise LlmCallError(
                "no_image_support", f"`{name}` 은 이미지를 받지 않는다고 정의돼 있다"
            )

        instruction = system_instruction
        request: dict[str, Any] = {}
        if definition.schema_mode == JSON_SCHEMA:
            send = client.chat.completions.parse
            request["response_format"] = response_schema
        elif definition.schema_mode == STRICT_TOOL:
            # 일반 답의 형식을 `json_object` 까지만 주는 회사가 있다. 그런 회사도 strict 도구
            # 인자는 스키마대로 낸다 — 도구 하나만 주고 그 도구로만 답하게 한다
            send = client.chat.completions.create
            request["tools"] = [answer_tool(response_schema)]
            request["tool_choice"] = {"type": "function", "function": {"name": ANSWER_TOOL}}
        else:
            send = client.chat.completions.create
            request["response_format"] = {"type": "json_object"}
            instruction = f"{system_instruction}\n\n{_json_shape(response_schema)}"
        if definition.temperature is not None:
            request["temperature"] = definition.temperature
        if definition.max_tokens is not None:
            request["max_tokens"] = definition.max_tokens
        if definition.extra_body:
            request["extra_body"] = definition.extra_body

        started = time.monotonic()
        try:
            response = await send(
                model=model,
                messages=[
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": _content(prompt, images)},
                ],
                **request,
            )
        except openai.APIError as exc:
            code = getattr(exc, "status_code", "없음")
            raise LlmCallError("api_error", f"{label} 호출 실패({code}): {exc}") from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        usage = _usage(name, model, response, latency_ms)
        log_usage(logger, kind, attempt, usage, _finish_reason(response))
        return _answer_text(response), usage

    return call_model


def answer_tool(response_schema: Any) -> dict[str, Any]:
    """답의 스키마를 strict 도구 하나로 바꾼다. 그 도구로만 답하게 하면 인자가 스키마대로 나온다."""
    if isinstance(response_schema, type) and issubclass(response_schema, BaseModel):
        # SDK 가 strict 규칙(모든 칸 required, additionalProperties false)으로 바꿔 준다
        tool = openai.pydantic_function_tool(response_schema, name=ANSWER_TOOL)
        parameters: dict[str, Any] = dict(tool["function"].get("parameters") or {})
    else:
        parameters = _schema_of(response_schema)
    return {
        "type": "function",
        "function": {
            "name": ANSWER_TOOL,
            "description": "정해진 모양으로 답을 낸다",
            "parameters": _without_annotations(parameters),
            "strict": True,
        },
    }


def _schema_of(response_schema: Any) -> dict[str, Any]:
    if isinstance(response_schema, type) and issubclass(response_schema, BaseModel):
        return response_schema.model_json_schema()
    if isinstance(response_schema, dict):
        return dict(response_schema)
    return {}


def _json_shape(response_schema: Any) -> str:
    """강제 없음일 때 지시에 붙이는 답의 모양. `json` 이라는 낱말이 있어야 받는 회사가 있다."""
    schema = _schema_of(response_schema)
    if not schema:
        return "JSON 으로만 답한다."
    return "JSON 으로만 답한다. 답은 아래 JSON Schema 모양을 지킨다.\n" + json.dumps(
        schema, ensure_ascii=False
    )


def _without_annotations(node: Any) -> Any:
    if isinstance(node, list):
        return [_without_annotations(item) for item in node]
    if not isinstance(node, dict):
        return node
    result: dict[str, Any] = {}
    for key, value in node.items():
        if key in _ANNOTATIONS:
            continue
        if key in _NAME_MAPS and isinstance(value, dict):
            # 이 사전의 열쇠는 속성 이름이다. 이름은 두고 그 아래 스키마만 다듬는다
            result[key] = {child: _without_annotations(schema) for child, schema in value.items()}
        else:
            result[key] = _without_annotations(value)
    return result


def _usage(name: str, model: str, response: Any, latency_ms: int) -> Usage:
    meta = getattr(response, "usage", None)
    return Usage(
        provider=name,
        model=model,
        input_tokens=_count(meta, "prompt_tokens"),
        output_tokens=_count(meta, "completion_tokens"),
        total_tokens=_count(meta, "total_tokens"),
        latency_ms=latency_ms,
    )


def _count(meta: Any, name: str) -> int:
    value = getattr(meta, name, None)
    return int(value) if value is not None else 0


def _choice(response: Any) -> Any:
    choices = getattr(response, "choices", None) or []
    return choices[0] if choices else None


def _finish_reason(response: Any) -> str:
    choice = _choice(response)
    if choice is None:
        return "없음"
    return str(getattr(choice, "finish_reason", "없음"))


def _text(response: Any) -> str:
    """본문을 꺼낸다. 막히거나 잘린 응답은 빈 문자열로 와서 파싱 실패가 된다."""
    choice = _choice(response)
    if choice is None:
        return ""
    return getattr(getattr(choice, "message", None), "content", None) or ""


def _answer_text(response: Any) -> str:
    """도구로 답했으면 그 인자, 아니면 본문. 둘 다 없으면 빈 문자열이라 파싱 실패가 된다."""
    choice = _choice(response)
    message = getattr(choice, "message", None)
    calls = getattr(message, "tool_calls", None) or []
    if calls:
        return str(getattr(getattr(calls[0], "function", None), "arguments", "") or "")
    return _text(response)
