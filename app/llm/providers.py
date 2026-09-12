"""어느 제공자를 쓸지 고르는 자리. 고르는 일만 하고 부르지는 않는다.

이름 하나를 항목 하나로 바꾸는 것이 전부다. 여기가 얇아야 "제공자로 분기하는 코드는 항목
안에만 있다" 가 참이 된다 (`.claude/rules/llm.md`).

**기능에 따라 다른 제공자를 쓴다.** 셀렉터 생성은 등록할 때 한 번이고 분류는 공고마다 한 번이라
성격이 다르다. 싼 제공자를 분류에 두고 정확한 제공자를 생성에 두는 선택이 가능해야 한다.
"""

from __future__ import annotations

from app.config import Settings
from app.llm.base import LlmCallError, Provider
from app.llm.claude import CLAUDE
from app.llm.gemini import GEMINI
from app.llm.log import CLASSIFY, IMAGE_READ, SELECTOR_GENERATE, SELECTOR_REPAIR
from app.llm.openai_compat import GPT_PROVIDER, OLLAMA_PROVIDER, QWEN_PROVIDER

PROVIDERS: dict[str, Provider] = {
    provider.name: provider
    for provider in (GEMINI, CLAUDE, GPT_PROVIDER, QWEN_PROVIDER, OLLAMA_PROVIDER)
}

# 응답이 스키마를 지키는 것에 기능이 걸려 있는 자리. 분류의 판정 칸이 닫힌 목록인 것이
# 여기 걸려 있다 (`app/classify/schema.py`).
#
# 셀렉터 생성은 여기 없다. 스키마를 벗어난 응답이 와도 `parse_selectors()` 가 거절하고 한 번
# 더 물어보는 길이 있어서다. 분류에는 그 길이 없다 — 목록에 없는 값이 와도 그럴듯해 보이고,
# 그대로 `normalized_jobs` 에 앉는다
NEEDS_SCHEMA: tuple[str, ...] = (CLASSIFY,)

# 기능마다 어느 설정이 제공자를 고르는가. 기능으로 갈리는 것은 여기까지고, 아래로는
# 제공자 항목이 자기 일을 한다
FEATURE_SETTING: dict[str, str] = {
    SELECTOR_GENERATE: "selector_generate_provider",
    SELECTOR_REPAIR: "selector_repair_provider",
    CLASSIFY: "classify_provider",
    IMAGE_READ: "image_read_provider",
}


def for_feature(feature: str, settings: Settings) -> tuple[Provider, str]:
    """이 기능이 쓸 항목과 모델 ID. 설정 두 줄을 읽는 일이 전부다.

    모델을 같이 돌려주는 것은 부르는 쪽이 `settings.gemini_model` 처럼 제공자 이름이 박힌
    칸을 직접 읽지 않게 하기 위해서다. 어느 칸에서 모델을 읽는지는 항목이 안다.
    """
    name = getattr(settings, FEATURE_SETTING[feature])
    provider = _named(name)
    model = str(getattr(settings, provider.model_setting))
    return resolve(feature, name, model), model


def resolve(feature: str, name: str, model: str) -> Provider:
    """기능·제공자 이름·모델로 항목을 고른다. 못 고르면 세운다.

    **다른 제공자로 넘어가지 않는다.** 조용히 넘어가면 비용 기록이 거짓말이 되고, 크레딧이
    떨어진 것을 아무도 모르는 채로 다른 계정에서 돈이 나간다 (`.claude/rules/llm.md`).
    """
    provider = _named(name)
    if feature in NEEDS_SCHEMA and not provider.forces_schema(model):
        raise LlmCallError(
            "no_schema_support",
            f"`{name}` 의 `{model}` 은 응답을 스키마로 강제하지 못한다. "
            f"{feature} 는 정해진 목록만 와야 해서 이 조합을 쓸 수 없다",
        )
    return provider


def _named(name: str) -> Provider:
    provider = PROVIDERS.get(name)
    if provider is None:
        raise LlmCallError(
            "unknown_provider",
            f"제공자 `{name}` 을 모른다. 쓸 수 있는 것: {', '.join(sorted(PROVIDERS))}",
        )
    return provider
