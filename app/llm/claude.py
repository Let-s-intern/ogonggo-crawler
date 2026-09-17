"""Claude 항목. `anthropic` SDK 하나만 이 파일이 안다.

`messages.parse()` 에 Pydantic 클래스를 그대로 넘긴다. SDK 가 JSON 스키마로 바꿔
`output_config.format` 으로 보내므로, 응답 스키마 클래스를 제공자마다 고쳐 둘 필요가 없다.

두 가지가 나머지 셋과 다르고, 둘 다 SDK 시그니처에서 확인한 것이다.

**`max_tokens` 가 필수다.** 나머지 셋은 안 줘도 된다. 이 값은 상한이지 청구액이 아니라
넉넉히 둔다 — 모자라면 응답이 잘려서 파싱 실패로 나타나고, 그 증상은 원인을 짐작하기 어렵다.

**`temperature` 가 없다.** `messages.create` 에도 `messages.parse` 에도 그런 인자가 없다.
그래서 받기는 하되 보내지 않는다. 부르는 쪽이 넘기는 값을 조용히 버리는 셈이라 여기 적어 둔다.
"""

from __future__ import annotations

import base64
import logging
import time
from collections.abc import Sequence
from typing import Any

import anthropic
from anthropic import AsyncAnthropic

from app.config import Settings
from app.llm.base import ImageInput, LlmCallError, Provider, Usage, log_usage

logger = logging.getLogger(__name__)

PROVIDER = "claude"

# 응답 길이의 상한. 청구는 실제로 쓴 토큰에만 붙으므로 이 값을 올려도 비용이 늘지 않는다.
# 낮게 잡으면 열네 칸짜리 분류 응답이 중간에서 잘리고, 그것은 깨진 JSON 으로 보인다
MAX_TOKENS = 8192


def build_client(settings: Settings) -> AsyncAnthropic:
    """API 키는 설정에서만 온다. 소스에도 로그에도 남기지 않는다.

    키를 반드시 넘긴다. 넘기지 않으면 SDK 가 환경의 `ANTHROPIC_API_KEY` 를 스스로 읽어,
    이 서비스가 설정한 적 없는 키로 호출이 나간다.
    """
    if not settings.claude_api_key:
        raise LlmCallError("no_api_key", "API 키가 없다")
    return AsyncAnthropic(api_key=settings.claude_api_key)


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

    `temperature` 는 받지만 보내지 않는다. Messages API 에 그 인자가 없다.
    """
    content: Any = prompt
    if images:
        # 이미지 블록을 앞에, 지시를 뒤에 둔다. 조각 순서가 곧 위에서 아래 순서다
        content = [
            *(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image.mime_type,
                        "data": base64.b64encode(image.data).decode("ascii"),
                    },
                }
                for image in images
            ),
            {"type": "text", "text": prompt},
        ]
    started = time.monotonic()
    try:
        response = await client.messages.parse(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system_instruction,
            messages=[{"role": "user", "content": content}],
            output_format=response_schema,
        )
    except anthropic.APIError as exc:
        # 운영자에게 보이는 문구에는 상태 코드와 메시지만 옮긴다. 키는 헤더로만 나간다.
        # 크레딧 소진에 전용 예외가 없어 429 로 오므로, 메시지를 남겨야 "한도인가 잔액인가"
        # 를 나중에 가를 수 있다
        code = getattr(exc, "status_code", "없음")
        raise LlmCallError("api_error", f"Claude 호출 실패({code}): {exc}") from exc

    latency_ms = int((time.monotonic() - started) * 1000)
    usage = _usage(model, response, latency_ms)
    log_usage(logger, kind, attempt, usage, _finish_reason(response))
    return _text(response), usage


async def list_models(client: Any) -> list[str]:
    """지금 부를 수 있는 모델 ID. 화면의 모델 칸을 채우는 데만 쓴다."""
    page = await client.models.list(limit=100)
    return [str(model.id) for model in getattr(page, "data", []) if getattr(model, "id", "")]


# 어느 모델이든 Structured Outputs 로 응답을 강제한다. 그래서 `schema_models` 를 두지 않는다
CLAUDE = Provider(
    name=PROVIDER,
    sdk="anthropic",
    key_setting="claude_api_key",
    model_setting="claude_model",
    build_client=build_client,
    call_model=call_model,
    list_models=list_models,
)


def _usage(model: str, response: Any, latency_ms: int) -> Usage:
    """입력과 출력만 온다. 합계 칸이 없어 여기서 더한다 — 넷 중 Claude 만 그렇다."""
    meta = getattr(response, "usage", None)
    input_tokens = _count(meta, "input_tokens")
    output_tokens = _count(meta, "output_tokens")
    return Usage(
        provider=PROVIDER,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        latency_ms=latency_ms,
    )


def _count(meta: Any, name: str) -> int:
    value = getattr(meta, name, None)
    return int(value) if value is not None else 0


def _finish_reason(response: Any) -> str:
    return str(getattr(response, "stop_reason", None) or "없음")


def _text(response: Any) -> str:
    """본문을 꺼낸다.

    응답은 블록의 목록이다. 글자를 담은 블록만 이어 붙이고, 하나도 없으면 빈 문자열이다 —
    막히거나 잘린 응답이 그렇게 오고, 그때는 파싱 실패가 된다.
    """
    blocks = getattr(response, "content", None) or []
    return "".join(getattr(block, "text", "") or "" for block in blocks)
