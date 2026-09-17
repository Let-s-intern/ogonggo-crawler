"""Gemini 항목. 클라이언트를 만들고, 한 번 부르고, 토큰을 센다.

이 파일이 아는 것은 Gemini 뿐이고, Gemini 를 아는 것도 이 파일뿐이다. 공통인 것 — 오류 타입,
비용 구조체, 로그 형식 — 은 `app/llm/base.py` 에 있다.

기능마다 다른 것은 셋뿐이다 — 시스템 지시, 응답 스키마, 로그에 적히는 이름(`kind`).
나머지(키를 어디서 읽는지, 어떤 예외를 어떻게 옮기는지, 무엇을 로그로 남기는지)는 같다.

API 키는 설정에서만 온다. 소스에도 로그에도 예외 메시지에도 남기지 않는다.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Any

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from app.config import Settings, get_settings
from app.llm.base import ImageInput, LlmCallError, Provider, Usage, log_usage

logger = logging.getLogger(__name__)

# 이 항목의 이름. `llm_calls.provider` 에 그대로 들어간다
PROVIDER = "gemini"


def build_client(settings: Settings | None = None) -> genai.Client:
    """API 키는 설정에서만 온다. 소스에도 로그에도 남기지 않는다."""
    resolved = settings or get_settings()
    if not resolved.gemini_api_key:
        raise LlmCallError("no_api_key", "API 키가 없다")
    return genai.Client(api_key=resolved.gemini_api_key)


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
    """호출 1회. 모델 ID·토큰·지연을 남긴다.

    `kind` 는 로그에서 기능을 가르는 이름이다. 같은 값이 `llm_calls.feature` 로도 들어가서,
    나중에 "무엇이 토큰을 썼나" 를 기능별로 셀 수 있다 (`app/llm/log.py`).
    """
    contents: Any = prompt
    if images:
        # 이미지를 앞에, 지시를 뒤에 둔다. 조각 순서가 곧 위에서 아래 순서다
        contents = [
            *(
                types.Part.from_bytes(data=image.data, mime_type=image.mime_type)
                for image in images
            ),
            prompt,
        ]
    started = time.monotonic()
    try:
        response = await client.aio.models.generate_content(
            model=model,
            contents=contents,
            config={
                "system_instruction": system_instruction,
                "response_mime_type": "application/json",
                "response_schema": response_schema,
                "temperature": temperature,
            },
        )
    except genai_errors.APIError as exc:
        # 운영자에게 보이는 문구에는 코드와 메시지만 옮긴다. 키는 헤더로만 나가므로 여기 없다.
        raise LlmCallError("api_error", f"Gemini 호출 실패({exc.code}): {exc.message}") from exc

    latency_ms = int((time.monotonic() - started) * 1000)
    usage = _usage(model, response, latency_ms)
    log_usage(logger, kind, attempt, usage, _finish_reason(response))
    return _text(response), usage


async def list_models(client: Any) -> list[str]:
    """지금 부를 수 있는 모델 ID. 화면의 모델 칸을 채우는 데만 쓴다.

    `models/` 접두사를 뗀다. 호출에 넣는 이름은 접두사 없는 쪽이라, 목록에서 고른 값이
    그대로 설정에 들어가야 한다.

    `generateContent` 를 못 하는 것은 뺀다. 임베딩과 `aqa` 가 목록에 같이 오는데 이 서비스가
    그것을 부를 일이 없고, 골라 놓으면 저장은 되고 호출만 실패한다. **거르는 기준을 모델
    이름이 아니라 API 가 말하는 것에 둔다** — 이름으로 거르면 그 규칙이 새 모델에서 틀린다.
    """
    names: list[str] = []
    async for model in await client.aio.models.list():
        name = str(getattr(model, "name", "") or "")
        actions = getattr(model, "supported_actions", None) or []
        if name and "generateContent" in actions:
            names.append(name.removeprefix("models/"))
    return names


# 어느 모델이든 `response_schema` 로 응답을 강제한다. 그래서 `schema_models` 를 두지 않는다
GEMINI = Provider(
    name=PROVIDER,
    sdk="google-genai",
    key_setting="gemini_api_key",
    model_setting="gemini_model",
    build_client=build_client,
    call_model=call_model,
    list_models=list_models,
)


def _usage(model: str, response: Any, latency_ms: int) -> Usage:
    meta = getattr(response, "usage_metadata", None)
    return Usage(
        provider=PROVIDER,
        model=model,
        input_tokens=_count(meta, "prompt_token_count"),
        output_tokens=_count(meta, "candidates_token_count"),
        total_tokens=_count(meta, "total_token_count"),
        latency_ms=latency_ms,
    )


def _count(meta: Any, name: str) -> int:
    value = getattr(meta, name, None)
    return int(value) if value is not None else 0


def _finish_reason(response: Any) -> str:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return "없음"
    return str(getattr(candidates[0], "finish_reason", "없음"))


def _text(response: Any) -> str:
    """본문을 꺼낸다. 막히거나 잘린 응답은 빈 문자열로 와서 파싱 실패가 된다."""
    return getattr(response, "text", None) or ""
