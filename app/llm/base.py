"""제공자와 무관한 것만 둔다. 어느 제공자인지 아는 코드는 제공자 항목뿐이다.

`app/llm/gemini.py` 하나였을 때는 오류 타입도 비용 구조체도 거기 있는 것이 자연스러웠다.
제공자가 넷이 되면 그것이 문제가 된다 — `app/llm/log.py` 가 `from app.llm.gemini import
PROVIDER` 로 상수를 가져다 박고 있었던 것처럼, 제공자 하나의 파일이 나머지 셋의 부모 노릇을
하게 된다. 그래서 공통인 것을 여기로 옮긴다.

**어댑터 탑을 쌓지 않는다** (`.claude/rules/llm.md`). 여기 있는 것은 셋뿐이다 — 호출이 실패한
방식(`LlmCallError`), 호출 1회의 비용(`Usage`), 그리고 제공자 항목이 무엇을 적어야 하는지
(`Provider`). 그 밖에 제공자로 갈리는 코드는 이 아래 어디에도 없다.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.config import Settings


class LlmCallError(RuntimeError):
    """호출을 시작하지 못했거나 응답 자체가 실패했다.

    | reason | 다음 행동 |
    |---|---|
    | `no_api_key` | 그 제공자의 키를 채운다. 서버 문제가 아니다 |
    | `api_error` | 응답 자체가 실패했다. 잠시 뒤 다시 |
    | `unknown_provider` | 설정에 적힌 제공자 이름이 없는 이름이다 |
    | `no_schema_support` | 응답을 스키마로 강제하지 못하는 제공자를 그런 기능에 지정했다 |

    부르는 기능마다 이 예외를 자기 예외로 옮겨 담는다. 화면에 나가는 문구가 기능마다 다르기
    때문이고, `reason` 은 그대로 넘긴다.
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Usage:
    """호출 1회의 비용. 이 숫자가 없으면 나중에 비용 질문에 답할 수 없다.

    `provider` 가 여기 있는 것은 **답한 제공자만이 이 값을 안다**는 이유 하나다. 기록하는
    쪽에서 설정을 다시 읽어 알아내면, 호출과 기록 사이에 설정이 바뀌었을 때 기록이 거짓이
    된다. 넷 중 어디에 돈이 나갔는지를 세는 칸이라 틀리면 세는 의미가 없다.
    """

    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    latency_ms: int


@dataclass(frozen=True)
class ImageInput:
    """호출에 함께 싣는 이미지 한 장. 바이트와 그 형식(`image/jpeg` 같은)이다.

    본문이 이미지로만 올라온 공고를 읽을 때만 쓴다 (`app/crawler/images.py`). 이미지가 없는
    호출은 지금까지와 똑같이 글자 하나만 보낸다.
    """

    data: bytes
    mime_type: str


class CallModel(Protocol):
    """제공자 항목이 내놓는 호출 1회.

    `response_schema` 는 Pydantic 클래스다. 그것을 자기 SDK 가 아는 모양으로 바꾸는 일이
    제공자마다 다르고, **그 차이가 사는 곳이 제공자 항목이다.**
    """

    def __call__(
        self,
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
    ) -> Awaitable[tuple[str, Usage]]: ...


class ListModels(Protocol):
    """제공자가 지금 주는 모델 ID 목록.

    화면의 모델 칸을 채우는 데만 쓴다. **모델 ID 를 소스에 적지 않기 위해서다**
    (`.claude/rules/llm.md`) — 제공자마다 모델이 뜨고 지고, 적어 둔 목록은 적은 날부터
    낡는다. 물어보면 오늘 있는 것이 온다.

    실패는 예외로 올린다. 목록을 못 받는 것이 호출을 막지는 않는다 — 부르는 쪽이 빈 목록으로
    떨어뜨리고 운영자는 모델 이름을 손으로 적으면 된다.
    """

    def __call__(self, client: Any) -> Awaitable[list[str]]: ...


@dataclass(frozen=True)
class Provider:
    """제공자 항목 하나. 여기 적힌 것 말고 어디에서도 제공자로 분기하지 않는다.

    `schema_models` 가 이 구조체에서 유일하게 설명이 필요한 칸이다. 나머지 셋은 어느 모델을
    쓰든 응답을 스키마로 강제하지만 **Qwen 은 일부 모델에서만 강제한다.** 그래서 "이 제공자가
    강제하는가" 가 아니라 "이 제공자가 이 모델에서 강제하는가" 를 물어야 한다.
    """

    # `llm_calls.provider` 에 그대로 들어간다
    name: str
    # 어느 패키지를 쓰는가. 사람이 읽는 값이고 코드가 이것으로 갈리지 않는다
    sdk: str
    # 이 제공자의 키와 모델 ID 를 담은 `Settings` 의 필드 이름
    key_setting: str
    model_setting: str
    build_client: Callable[[Settings], Any]
    call_model: CallModel
    # 화면의 모델 칸을 채울 목록을 어떻게 받아 오는가. 없으면 손으로 적는다
    list_models: ListModels | None = None
    # 응답을 스키마로 강제하는 모델. `None` 은 모든 모델이 강제한다는 뜻이다.
    # 값이 있으면 그것으로 시작하는 모델만 강제한다 — 문서가 지원을 시리즈 단위로 적는다
    schema_models: tuple[str, ...] | None = None
    # 이미지를 함께 받는가. 코드에 든 다섯은 받는다고 두어 지금까지와 같고, 화면에서 추가한
    # 회사는 정의가 정한다 (`app/llm/custom.py`)
    images: bool = True
    # 키와 모델을 어디서 읽는가. 비어 있으면 `key_setting`·`model_setting` 이 가리키는 설정
    # 칸이다. 화면에서 추가한 회사는 이름이 정해져 있지 않아 칸을 미리 만들 수 없으므로 이
    # 함수로 설정의 사전에서 읽는다
    read_key: Callable[[Settings], str] | None = None
    read_model: Callable[[Settings], str] | None = None

    def key_of(self, settings: Settings) -> str:
        """이 제공자의 키. 설정에서만 읽는다."""
        if self.read_key is not None:
            return self.read_key(settings)
        return str(getattr(settings, self.key_setting))

    def model_of(self, settings: Settings) -> str:
        """이 제공자로 부를 모델 ID. 설정에서만 읽는다."""
        if self.read_model is not None:
            return self.read_model(settings)
        return str(getattr(settings, self.model_setting))

    def forces_schema(self, model: str) -> bool:
        """이 모델이 응답을 스키마대로 내는 것을 보장하는가.

        보장하지 못하는 조합을 분류에 쓰면 판정 칸의 닫힌 목록이 부탁으로 내려앉는다.
        부탁은 대개 지켜지고, 대개는 640건에서 스무 건쯤 어긋난다는 뜻이다
        (`app/classify/schema.py`).
        """
        if self.schema_models is None:
            return True
        return model.startswith(self.schema_models)


def log_usage(
    log: logging.Logger,
    kind: str,
    attempt: int,
    usage: Usage,
    finish_reason: str,
) -> None:
    """호출 하나를 로그 한 줄로 남긴다. 형식이 제공자마다 갈리지 않도록 여기 한 줄만 둔다.

    로거는 부르는 쪽이 넘긴다. 기록이 남는 이름이 `app.llm.base` 가 아니라 그 제공자의
    모듈 이름이어야, 로그만 보고도 어느 제공자가 답했는지 알 수 있다.
    """
    log.info(
        "%s model=%s provider=%s attempt=%d input_tokens=%d output_tokens=%d "
        "total_tokens=%d latency_ms=%d finish_reason=%s",
        kind,
        usage.model,
        usage.provider,
        attempt,
        usage.input_tokens,
        usage.output_tokens,
        usage.total_tokens,
        usage.latency_ms,
        finish_reason,
    )
