"""기능마다 다른 제공자를 고를 수 있는지 (1.6.V).

**실제로 부르지 않는다.** 제공자 항목을 기록만 하는 가짜로 갈아 끼우고, 셋을 확인한다 —
셀렉터 생성·셀렉터 수정·본문 분류가 각자 지정된 항목을 부르는가, 그 항목의 모델 ID 를
쓰는가, 그리고 없는 이름을 지정하면 서는가.

이것이 이 Push 의 목적 그 자체다. 분류가 비용의 대부분이라 거기만 싼 제공자로 옮기는 선택이
가능해야 하고, 그러려면 세 기능이 서로 다른 항목을 부를 수 있어야 한다.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from app.classify.classifier import ClassifyError, classify_body
from app.classify.schema import Classification
from app.config import Settings
from app.llm.base import Usage
from app.llm.providers import PROVIDERS
from app.selector.generator import SelectorGenerationError, generate_from_html
from app.selector.repair import repair_from_html
from app.selector.schema import validate_selectors
from tests.test_classify_body import BODY, response
from tests.test_selector_generator import DETAIL_HTML, LIST_HTML, VALID_RESPONSE
from tests.test_selector_repair import BROKEN, DETAIL_URL, LIST_URL
from tests.test_selector_repair import DETAIL_HTML as REPAIR_DETAIL_HTML
from tests.test_selector_repair import LIST_HTML as REPAIR_LIST_HTML
from tests.test_selector_repair import response as repair_response

CLASSIFICATION = response(work_location="판교")

# 기록기가 돌려줄 답. 세 기능이 기대하는 모양이 다 달라서 무엇으로 물었는지를 보고 고른다 —
# 제공자마다 답을 하나로 고정하면 셀렉터 자리에 분류 응답이 돌아온다
REPAIRED = repair_response()


def _answer(response_schema: Any, kind: str) -> str:
    if response_schema is Classification:
        return CLASSIFICATION
    return REPAIRED if kind == "셀렉터 고치기" else VALID_RESPONSE


class Recorder:
    """부른 사실만 남기는 가짜 제공자 항목.

    한 제공자를 여러 기능이 같이 쓸 수 있어서, 답은 `_answer()` 가 고른다.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.clients: list[str] = []
        self.calls: list[dict[str, Any]] = []

    def build_client(self, settings: Settings) -> str:
        self.clients.append(self.name)
        return f"{self.name}-client"

    async def call_model(
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
    ) -> tuple[str, Usage]:
        self.calls.append({"model": model, "kind": kind, "client": client})
        return _answer(response_schema, kind), Usage(
            provider=self.name,
            model=model,
            input_tokens=10,
            output_tokens=2,
            total_tokens=12,
            latency_ms=1,
        )


@pytest.fixture
def recorders(monkeypatch: pytest.MonkeyPatch) -> dict[str, Recorder]:
    """네 항목을 전부 기록기로 갈아 끼운다. 이름과 모델 설정은 그대로 둔다."""
    made: dict[str, Recorder] = {}
    for name, provider in PROVIDERS.items():
        recorder = Recorder(name)
        made[name] = recorder
        monkeypatch.setitem(
            PROVIDERS,
            name,
            replace(provider, build_client=recorder.build_client, call_model=recorder.call_model),
        )
    return made


def settings_for(**chosen: str) -> Settings:
    """네 제공자 키를 다 채운 설정. 어느 것을 골라도 키 때문에 서지 않는다."""
    return Settings(
        gemini_api_key="키",
        claude_api_key="키",
        gpt_api_key="키",
        qwen_api_key="키",
        **chosen,
    )


async def test_generating_selectors_calls_the_provider_it_was_pointed_at(
    recorders: dict[str, Recorder],
) -> None:
    settings = settings_for(selector_generate_provider="claude")

    await generate_from_html(LIST_HTML, DETAIL_HTML, settings=settings)

    assert len(recorders["claude"].calls) == 1
    assert recorders["claude"].calls[0]["model"] == settings.claude_model
    assert recorders["claude"].calls[0]["client"] == "claude-client"
    assert recorders["gemini"].calls == []


async def test_classifying_calls_a_different_provider_at_the_same_time(
    recorders: dict[str, Recorder],
) -> None:
    """이 조합이 이 Push 를 하는 이유다 — 비싼 쪽은 정확하게, 많은 쪽은 싸게."""
    settings = settings_for(selector_generate_provider="claude", classify_provider="qwen")

    await generate_from_html(LIST_HTML, DETAIL_HTML, settings=settings)
    await classify_body(BODY, settings=settings)

    assert [call["model"] for call in recorders["claude"].calls] == [settings.claude_model]
    assert [call["model"] for call in recorders["qwen"].calls] == [settings.qwen_model]
    assert recorders["gemini"].calls == []
    assert recorders["gpt"].calls == []


async def test_each_feature_reads_the_model_of_its_own_provider(
    recorders: dict[str, Recorder],
) -> None:
    """모델 ID 를 제공자와 따로 고르지 않는다. 항목이 자기 칸을 안다."""
    settings = settings_for(classify_provider="gpt", gpt_model="gpt-5.6-terra")

    await classify_body(BODY, settings=settings)

    assert recorders["gpt"].calls[0]["model"] == "gpt-5.6-terra"


async def test_nothing_chosen_still_goes_to_gemini(recorders: dict[str, Recorder]) -> None:
    """지금까지 쓰던 제공자다. 설정을 안 건드린 배포가 그대로 돌아야 한다."""
    await classify_body(BODY, settings=settings_for())

    assert len(recorders["gemini"].calls) == 1
    assert recorders["gemini"].calls[0]["model"] == "gemini-3.5-flash"


async def test_a_provider_name_nobody_defined_stops_the_feature(
    recorders: dict[str, Recorder],
) -> None:
    with pytest.raises(ClassifyError) as caught:
        await classify_body(BODY, settings=settings_for(classify_provider="없는것"))

    assert caught.value.reason == "unknown_provider"
    assert recorders["gemini"].calls == []


async def test_pointing_classification_at_a_model_that_cannot_force_a_schema_stops_it(
    recorders: dict[str, Recorder],
) -> None:
    """별칭은 `json_object` 까지만 된다. 판정 칸의 닫힌 목록이 부탁으로 내려앉는다."""
    settings = settings_for(classify_provider="qwen", qwen_model="qwen-plus")

    with pytest.raises(ClassifyError) as caught:
        await classify_body(BODY, settings=settings)

    assert caught.value.reason == "no_schema_support"
    assert recorders["qwen"].calls == []


async def test_the_same_model_is_fine_for_generating_selectors(
    recorders: dict[str, Recorder],
) -> None:
    """생성은 응답을 다시 검증하고 한 번 더 물어보는 길이 있다."""
    settings = settings_for(selector_generate_provider="qwen", qwen_model="qwen-plus")

    await generate_from_html(LIST_HTML, DETAIL_HTML, settings=settings)

    assert recorders["qwen"].calls[0]["model"] == "qwen-plus"


async def test_a_missing_key_stops_that_feature_and_does_not_move_to_another_provider() -> None:
    """기록기를 끼우지 않는다. 진짜 항목이 키를 보고 서야 한다."""
    with pytest.raises(SelectorGenerationError) as caught:
        await generate_from_html(
            LIST_HTML,
            DETAIL_HTML,
            settings=Settings(selector_generate_provider="qwen", qwen_api_key=""),
        )

    assert caught.value.reason == "no_api_key"
    assert "QWEN_API_KEY" in str(caught.value)


async def test_fixing_selectors_calls_its_own_provider_not_the_generators(
    recorders: dict[str, Recorder],
) -> None:
    """생성과 고치기는 서로 다른 제공자를 고를 수 있다. 같은 파일을 쓰지만 설정이 다르다."""
    settings = settings_for(selector_generate_provider="claude", selector_repair_provider="gpt")

    await repair_from_html(
        REPAIR_LIST_HTML,
        REPAIR_DETAIL_HTML,
        validate_selectors(BROKEN),
        list_url=LIST_URL,
        detail_url=DETAIL_URL,
        settings=settings,
    )

    assert [call["model"] for call in recorders["gpt"].calls] == [settings.gpt_model]
    assert recorders["claude"].calls == []
    assert recorders["gemini"].calls == []


def test_the_recorded_answers_are_the_shapes_the_parsers_expect() -> None:
    """가짜 응답이 스키마를 벗어나면 위의 테스트가 엉뚱한 이유로 통과한다."""
    assert json.loads(VALID_RESPONSE)["list"]
    assert json.loads(CLASSIFICATION)["work_location"][0]["text"] == "판교"
