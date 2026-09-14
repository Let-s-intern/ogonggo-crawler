"""화면에서 추가한 OpenAI 호환 회사 (2026-09-14).

**실제로 부르지 않는다.** 가짜 클라이언트가 답하고, 확인하는 것은 넷이다.

- 정의가 깐깐하게 검증되는가. 틀린 정의가 저장되면 그 사실은 다음 분류가 돌 때에야 드러난다.
- 회사마다 다른 요청 모양(답 형식 강제·더할 값·온도·길이 상한·이미지)이 정의대로 나가는가.
- 기능에 지정할 때 거절 규칙이 코드에 든 제공자와 같게 걸리는가. 강제 없는 회사는 셀렉터
  생성과 AI 수정에만, 이미지를 안 받는 회사는 이미지 읽기에 못 쓴다.
- 저장한 회사가 `settings_for` → `for_feature` 로 코드에 든 제공자와 똑같이 불리는가.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator, Sequence
from typing import Any

import httpx
import openai
import pytest
from pydantic import BaseModel

from app import db
from app.api.import_data import import_database
from app.config import Settings
from app.llm import custom
from app.llm import settings as store
from app.llm.base import ImageInput, LlmCallError, Usage
from app.llm.log import CLASSIFY, IMAGE_READ, SELECTOR_GENERATE, SELECTOR_REPAIR
from app.llm.openai_compat import answer_tool, custom_entry
from app.llm.providers import for_feature, resolve
from tests.test_import_merge import job, make_upload
from tests.test_llm_settings import env

KEY = "sk-딥시크-화면-키-5678"
ANSWER = '{"title": "답", "count": 1}'
CHECK_ANSWER = '{"ok": true, "word": "확인"}'


class Answer(BaseModel):
    """`title` 이라는 칸이 있는 스키마. strict 도구 스키마에서 주석만 떼고 칸은 남는지 본다."""

    title: str
    count: int = 0


class FakeUsage:
    prompt_tokens = 100
    completion_tokens = 20
    total_tokens = 120


class FakeFunction:
    def __init__(self, arguments: str) -> None:
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, arguments: str) -> None:
        self.function = FakeFunction(arguments)


class FakeMessage:
    def __init__(self, content: str | None, tool_arguments: str | None) -> None:
        self.content = content
        self.tool_calls = [FakeToolCall(tool_arguments)] if tool_arguments is not None else None


class FakeChoice:
    def __init__(self, message: FakeMessage) -> None:
        self.message = message
        self.finish_reason = "stop"


class FakeResponse:
    def __init__(self, message: FakeMessage) -> None:
        self.choices = [FakeChoice(message)]
        self.usage = FakeUsage()


class FakeCompletions:
    """`parse` 와 `create` 를 흉내내고, 어느 쪽으로 무엇이 나갔는지 남긴다."""

    def __init__(
        self, content: str | None, tool_arguments: str | None, error: Exception | None
    ) -> None:
        self._content = content
        self._tool_arguments = tool_arguments
        self._error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def parse(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(("parse", kwargs))
        return self._answer()

    async def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(("create", kwargs))
        return self._answer()

    def _answer(self) -> FakeResponse:
        if self._error is not None:
            raise self._error
        return FakeResponse(FakeMessage(self._content, self._tool_arguments))


class FakeClient:
    def __init__(
        self,
        content: str | None = ANSWER,
        tool_arguments: str | None = None,
        error: Exception | None = None,
    ) -> None:
        self.completions = FakeCompletions(content, tool_arguments, error)
        self.chat = self

    @property
    def calls(self) -> list[tuple[str, dict[str, Any]]]:
        return self.completions.calls


def connection_error() -> Exception:
    return openai.APIConnectionError(request=httpx.Request("POST", "https://api.deepseek.com"))


def definition(**overrides: Any) -> custom.CustomDefinition:
    values: dict[str, Any] = {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/beta",
        "schema_mode": custom.STRICT_TOOL,
        **overrides,
    }
    return custom.CustomDefinition.model_validate(values)


async def call(
    defined: custom.CustomDefinition,
    client: FakeClient,
    images: Sequence[ImageInput] = (),
) -> tuple[str, Usage]:
    return await custom_entry("deepseek", defined).call_model(
        client,
        "deepseek-flash",
        "본문",
        1,
        "본문 분류",
        response_schema=Answer,
        system_instruction="지시",
        temperature=0.0,
        images=images,
    )


def sent(client: FakeClient) -> tuple[str, dict[str, Any]]:
    assert len(client.calls) == 1
    return client.calls[0]


# 부르는 모양 ----------------------------------------------------------------------------


async def test_strict_도구_호출은_스키마를_도구_하나로_넘기고_그_도구로만_답하게_한다() -> None:
    client = FakeClient(content=None, tool_arguments=ANSWER)

    text, usage = await call(definition(), client)

    method, request = sent(client)
    assert method == "create"
    assert request["tools"][0]["function"]["strict"] is True
    assert request["tool_choice"] == {"type": "function", "function": {"name": "answer"}}
    assert "response_format" not in request
    assert text == ANSWER
    assert (usage.provider, usage.model, usage.total_tokens) == ("deepseek", "deepseek-flash", 120)


def test_strict_스키마에서_주석은_떼고_title_이라는_칸은_남긴다() -> None:
    parameters = answer_tool(Answer)["function"]["parameters"]

    assert set(parameters["properties"]) == {"title", "count"}
    assert "title" not in parameters
    for schema in parameters["properties"].values():
        assert "title" not in schema
        assert "default" not in schema


async def test_json_schema_는_response_format_으로_보낸다() -> None:
    client = FakeClient()

    text, _ = await call(definition(schema_mode=custom.JSON_SCHEMA), client)

    method, request = sent(client)
    assert method == "parse"
    assert request["response_format"] is Answer
    assert "tools" not in request
    assert text == ANSWER


async def test_강제_없음은_json_object_로_보내고_지시에_모양을_붙인다() -> None:
    client = FakeClient()

    await call(definition(schema_mode=custom.NO_SCHEMA), client)

    method, request = sent(client)
    assert method == "create"
    assert request["response_format"] == {"type": "json_object"}
    instruction = request["messages"][0]["content"]
    assert instruction.startswith("지시")
    assert "JSON" in instruction
    assert '"title"' in instruction


async def test_부르는_쪽의_온도는_쓰지_않고_정의가_정한다() -> None:
    unset = FakeClient(content=None, tool_arguments=ANSWER)
    fixed = FakeClient(content=None, tool_arguments=ANSWER)

    await call(definition(), unset)
    await call(definition(temperature=0.6), fixed)

    assert "temperature" not in sent(unset)[1]
    assert sent(fixed)[1]["temperature"] == 0.6


async def test_길이_상한과_더할_값을_정의대로_보내고_없으면_보내지_않는다() -> None:
    plain = FakeClient(content=None, tool_arguments=ANSWER)
    tuned = FakeClient(content=None, tool_arguments=ANSWER)

    await call(definition(), plain)
    await call(definition(max_tokens=32768, extra_body={"thinking": {"type": "disabled"}}), tuned)

    assert "max_tokens" not in sent(plain)[1]
    assert "extra_body" not in sent(plain)[1]
    assert sent(tuned)[1]["max_tokens"] == 32768
    assert sent(tuned)[1]["extra_body"] == {"thinking": {"type": "disabled"}}


async def test_이미지는_데이터_주소로_글보다_앞에_붙는다() -> None:
    client = FakeClient(content=None, tool_arguments=ANSWER)

    await call(definition(images=True), client, images=[ImageInput(b"png", "image/png")])

    content = sent(client)[1]["messages"][1]["content"]
    assert content[0]["type"] == "image_url"
    assert content[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert content[-1] == {"type": "text", "text": "본문"}


async def test_이미지를_받지_않는_회사에_이미지를_보내면_부르지_않고_선다() -> None:
    client = FakeClient()

    with pytest.raises(LlmCallError) as caught:
        await call(definition(images=False), client, images=[ImageInput(b"png", "image/png")])

    assert caught.value.reason == "no_image_support"
    assert client.calls == []


async def test_API_실패는_회사_이름이_붙은_우리_오류로_바뀐다() -> None:
    with pytest.raises(LlmCallError) as caught:
        await call(definition(), FakeClient(error=connection_error()))

    assert caught.value.reason == "api_error"
    assert "DeepSeek" in str(caught.value)


def test_모델_목록은_정의에서_켰을_때만_받는다() -> None:
    assert custom_entry("deepseek", definition(list_models=False)).list_models is None
    assert custom_entry("deepseek", definition(list_models=True)).list_models is not None


class FakeModel:
    def __init__(self, model_id: str) -> None:
        self.id = model_id


class FakeModelsPage:
    def __init__(self, ids: list[str]) -> None:
        self.data = [FakeModel(model_id) for model_id in ids]


class FakeLister:
    """`client.models.list()` 와 `with_options(base_url=...)` 만 흉내낸다.

    어느 주소에 물었는지 남긴다.
    """

    def __init__(self, base_url: str, asked: list[str]) -> None:
        self.base_url = base_url
        self.asked = asked
        self.models = self

    def with_options(self, **kwargs: Any) -> FakeLister:
        return FakeLister(kwargs["base_url"], self.asked)

    async def list(self) -> FakeModelsPage:
        self.asked.append(self.base_url)
        return FakeModelsPage(["deepseek-flash"])


@pytest.mark.parametrize(
    ("models_url", "expected"),
    [
        ("https://api.deepseek.com", "https://api.deepseek.com"),
        ("", "https://api.deepseek.com/beta"),
    ],
)
async def test_모델_목록은_목록_주소가_있으면_그_주소에서_없으면_호출_주소에서_받는다(
    models_url: str, expected: str
) -> None:
    asked: list[str] = []
    lister = custom_entry("deepseek", definition(models_url=models_url)).list_models

    assert lister is not None
    assert await lister(FakeLister("https://api.deepseek.com/beta", asked)) == ["deepseek-flash"]
    assert asked == [expected]


def test_키는_설정의_사전에서만_읽고_없으면_여기서_선다() -> None:
    provider = custom_entry("deepseek", definition())

    with pytest.raises(LlmCallError) as caught:
        provider.build_client(Settings())
    client = provider.build_client(Settings(llm_custom_keys={"deepseek": KEY}))

    assert caught.value.reason == "no_api_key"
    assert client.api_key == KEY
    assert str(client.base_url).startswith("https://api.deepseek.com/beta")


# 정의 검증 --------------------------------------------------------------------------------


def form(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/beta",
        "schema_mode": "strict_tool",
        "temperature": "",
        "max_tokens": "32768",
        "images": True,
        "list_models": True,
        "models_url": "",
        "extra_body": '{"thinking": {"type": "disabled"}}',
        "prices": '{"deepseek-flash": {"input": 0.15, "output": 0.6}}',
        **overrides,
    }
    return values


@pytest.mark.parametrize("name", ["gemini", "ollama", "DeepSeek", "d", "딥시크", "deep seek"])
def test_이름은_좁게_받고_코드에_든_제공자_이름은_거절한다(name: str) -> None:
    with pytest.raises(custom.CustomDefinitionError):
        custom.check_name(name, {"gemini", "claude", "gpt", "qwen", "ollama"})


def test_규칙에_맞는_이름은_앞뒤_공백만_걷고_받는다() -> None:
    assert custom.check_name("  deep-seek_2 ", {"gemini"}) == "deep-seek_2"


@pytest.mark.parametrize(
    "overrides",
    [
        {"base_url": "api.deepseek.com"},
        {"schema_mode": "xml"},
        {"temperature": "뜨겁게"},
        {"temperature": "3"},
        {"max_tokens": "0"},
        {"extra_body": "[1, 2]"},
        {"extra_body": "{깨짐"},
        {"prices": '{"deepseek-flash": {"input": -1, "output": 1}}'},
        {"prices": '{"deepseek-flash": {"in": 1}}'},
        {"models_url": "api.deepseek.com"},
    ],
)
def test_폼_값이_틀리면_거절한다(overrides: dict[str, Any]) -> None:
    with pytest.raises(custom.CustomDefinitionError):
        custom.from_form(**form(**overrides))


def test_빈_칸은_보내지_않음이다() -> None:
    defined = custom.from_form(**form(temperature="", max_tokens="", extra_body="", prices=""))

    assert defined.temperature is None
    assert defined.max_tokens is None
    assert defined.extra_body == {}
    assert defined.prices == {}


# 이름으로 항목 찾기 ---------------------------------------------------------------------------


def settings_with(**overrides: Any) -> Settings:
    return Settings(llm_custom_providers={"deepseek": custom.to_json(definition(**overrides))})


@pytest.mark.parametrize("feature", [CLASSIFY, IMAGE_READ])
def test_강제_없는_회사는_분류와_이미지_읽기에_쓸_수_없다(feature: str) -> None:
    settings = settings_with(schema_mode=custom.NO_SCHEMA, images=True)

    with pytest.raises(LlmCallError) as caught:
        resolve(feature, "deepseek", "deepseek-flash", settings)

    assert caught.value.reason == "no_schema_support"


@pytest.mark.parametrize("feature", [SELECTOR_GENERATE, SELECTOR_REPAIR])
def test_강제_없는_회사도_셀렉터_생성과_AI_수정에는_쓴다(feature: str) -> None:
    settings = settings_with(schema_mode=custom.NO_SCHEMA)

    assert resolve(feature, "deepseek", "deepseek-flash", settings).name == "deepseek"


def test_이미지를_받지_않는_회사는_이미지_읽기에_쓸_수_없다() -> None:
    settings = settings_with(images=False)

    with pytest.raises(LlmCallError) as caught:
        resolve(IMAGE_READ, "deepseek", "deepseek-flash", settings)

    assert caught.value.reason == "no_image_support"
    assert resolve(CLASSIFY, "deepseek", "deepseek-flash", settings).name == "deepseek"


def test_아무도_정의하지_않은_이름은_선다() -> None:
    with pytest.raises(LlmCallError) as caught:
        resolve(CLASSIFY, "kimi", "kimi-k2.6", settings_with())

    assert caught.value.reason == "unknown_provider"


def test_깨진_정의는_invalid_provider_로_선다() -> None:
    settings = Settings(llm_custom_providers={"deepseek": "{깨짐"})

    with pytest.raises(LlmCallError) as caught:
        resolve(CLASSIFY, "deepseek", "deepseek-flash", settings)

    assert caught.value.reason == "invalid_provider"


# 저장소 --------------------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    try:
        yield connection
    finally:
        connection.close()


def rows(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row["key"]): str(row["value"])
        for row in conn.execute("SELECT key, value FROM app_settings")
    }


def save(conn: sqlite3.Connection, name: str = "deepseek", **overrides: Any) -> store.LlmConfig:
    return store.write_custom(conn, name, **form(**overrides), settings=env())


def ready(conn: sqlite3.Connection, **overrides: Any) -> None:
    """정의를 저장하고 키까지 넣는다."""
    save(conn, **overrides)
    store.write_key(conn, "deepseek", KEY, env())


def test_추가한_회사가_키_표와_제공자_목록과_회사_목록에_나온다(conn: sqlite3.Connection) -> None:
    config = save(conn)

    assert "deepseek" in config.provider_names
    assert config.key("deepseek").present is False
    (item,) = config.customs
    assert (item.label, item.schema_mode, item.max_tokens) == ("DeepSeek", "strict_tool", "32768")
    assert item.test_model == "deepseek-flash"
    assert json.loads(rows(conn)[store.custom_row("deepseek")])["schema_mode"] == "strict_tool"


def test_지정한_기능이_저장된_키와_모델로_추가한_회사를_부른다(conn: sqlite3.Connection) -> None:
    ready(conn)
    store.write_feature(conn, CLASSIFY, "deepseek", "deepseek-flash", env())

    resolved = store.settings_for(conn, CLASSIFY, env())
    provider, model = for_feature(CLASSIFY, resolved)

    assert provider.name == "deepseek"
    assert model == "deepseek-flash"
    assert provider.key_of(resolved) == KEY
    assert provider.build_client(resolved).api_key == KEY


def test_코드에_든_제공자를_쓰는_기능은_그대로다(conn: sqlite3.Connection) -> None:
    ready(conn)

    resolved = store.settings_for(conn, SELECTOR_GENERATE, env())
    provider, model = for_feature(SELECTOR_GENERATE, resolved)

    assert provider.name == "gemini"
    assert model == resolved.gemini_model


def test_추가한_회사는_모델을_비우고_지정할_수_없다(conn: sqlite3.Connection) -> None:
    ready(conn)

    with pytest.raises(store.LlmSettingError, match="모델"):
        store.write_feature(conn, CLASSIFY, "deepseek", "", env())


def test_키_없는_추가한_회사는_기능에_지정할_수_없다(conn: sqlite3.Connection) -> None:
    save(conn)

    with pytest.raises(store.LlmSettingError, match="API 키가 없다"):
        store.write_feature(conn, CLASSIFY, "deepseek", "deepseek-flash", env())


def test_강제_없음으로_저장한_회사는_분류에는_못_쓰고_셀렉터_생성에는_쓴다(
    conn: sqlite3.Connection,
) -> None:
    ready(conn, schema_mode="none")

    with pytest.raises(store.LlmSettingError):
        store.write_feature(conn, CLASSIFY, "deepseek", "deepseek-flash", env())
    store.write_feature(conn, SELECTOR_GENERATE, "deepseek", "deepseek-flash", env())

    assert rows(conn)[store.provider_row(SELECTOR_GENERATE)] == "deepseek"


def test_이미지_안_받는_회사는_이미지_읽기에_지정할_수_없다(conn: sqlite3.Connection) -> None:
    ready(conn, images=False)

    with pytest.raises(store.LlmSettingError, match="이미지"):
        store.write_feature(conn, IMAGE_READ, "deepseek", "deepseek-flash", env())


def test_쓰는_기능을_못_쓰게_만드는_정의_변경은_거절한다(conn: sqlite3.Connection) -> None:
    ready(conn)
    store.write_feature(conn, CLASSIFY, "deepseek", "deepseek-flash", env())

    with pytest.raises(store.LlmSettingError, match="본문 분류"):
        save(conn, schema_mode="none")

    assert json.loads(rows(conn)[store.custom_row("deepseek")])["schema_mode"] == "strict_tool"


def test_쓰는_기능이_있으면_지우지_못한다(conn: sqlite3.Connection) -> None:
    ready(conn)
    store.write_feature(conn, CLASSIFY, "deepseek", "deepseek-flash", env())

    with pytest.raises(store.LlmSettingError, match="쓰고 있다"):
        store.delete_custom(conn, "deepseek", env())

    assert store.custom_row("deepseek") in rows(conn)


def test_지우면_정의와_키가_같이_사라진다(conn: sqlite3.Connection) -> None:
    ready(conn)

    config = store.delete_custom(conn, "deepseek", env())

    assert store.custom_row("deepseek") not in rows(conn)
    assert store.key_row("deepseek") not in rows(conn)
    assert "deepseek" not in config.provider_names


def test_코드에_든_제공자는_지울_수_없다(conn: sqlite3.Connection) -> None:
    with pytest.raises(store.LlmSettingError):
        store.delete_custom(conn, "gemini", env())


def test_정의가_깨져_있어도_읽기는_서지_않고_사유를_보여_준다(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?)",
        (store.custom_row("deepseek"), "{깨짐"),
    )

    config = store.read_config(conn, env())

    (item,) = config.customs
    assert item.problem
    assert "deepseek" not in config.provider_names


# 연결 테스트 ---------------------------------------------------------------------------------


def calls(conn: sqlite3.Connection) -> list[tuple[str, str, str, int]]:
    return [
        (str(row["provider"]), str(row["model"]), str(row["feature"]), int(row["ok"]))
        for row in conn.execute("SELECT provider, model, feature, ok FROM llm_calls ORDER BY id")
    ]


async def test_연결_테스트는_작은_답을_받고_호출_기록에_남긴다(conn: sqlite3.Connection) -> None:
    ready(conn)
    client = FakeClient(content=None, tool_arguments=CHECK_ANSWER)

    result = await store.check_connection(conn, "deepseek", "deepseek-flash", env(), client=client)

    assert result.ok is True
    assert "120" in result.message
    assert calls(conn) == [("deepseek", "deepseek-flash", store.CONNECTION_TEST, 1)]


async def test_연결_테스트가_실패하면_사유를_돌려주고_실패도_남긴다(
    conn: sqlite3.Connection,
) -> None:
    ready(conn)

    result = await store.check_connection(
        conn, "deepseek", "deepseek-flash", env(), client=FakeClient(error=connection_error())
    )

    assert result.ok is False
    assert calls(conn) == [("deepseek", "deepseek-flash", store.CONNECTION_TEST, 0)]


async def test_모양이_다른_답은_연결_테스트_실패다(conn: sqlite3.Connection) -> None:
    ready(conn)
    client = FakeClient(content=None, tool_arguments='{"wrong": 1}')

    result = await store.check_connection(conn, "deepseek", "deepseek-flash", env(), client=client)

    assert result.ok is False
    assert "정해진 모양" in result.message
    assert calls(conn) == [("deepseek", "deepseek-flash", store.CONNECTION_TEST, 1)]


async def test_모델을_적지_않으면_연결_테스트를_하지_않는다(conn: sqlite3.Connection) -> None:
    ready(conn)

    with pytest.raises(store.LlmSettingError, match="모델"):
        await store.check_connection(conn, "deepseek", " ", env(), client=FakeClient())

    assert calls(conn) == []


# 가져오기 -----------------------------------------------------------------------------------


def test_가져오기에_추가한_회사의_정의와_키가_따라온다(
    conn: sqlite3.Connection, tmp_path: pathlib.Path
) -> None:
    path = make_upload(tmp_path / "upload.db", jobs=[job("공고")])
    source = db.connect(path)
    source.executemany(
        "INSERT INTO app_settings (key, value) VALUES (?, ?)",
        [
            (store.custom_row("deepseek"), custom.to_json(definition())),
            (store.key_row("deepseek"), "sk-저쪽-서버의-키-9999"),
        ],
    )
    source.close()

    result = import_database(conn, path)

    assert result.llm_added == 2
    config = store.read_config(conn, env())
    assert "deepseek" in config.provider_names
    assert config.key("deepseek").tail == "9999"
