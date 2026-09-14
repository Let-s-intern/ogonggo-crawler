"""AI 설정 화면의 "직접 추가한 회사" (2026-09-14).

화면이 지켜야 하는 것을 잠근다. 추가한 회사는 키 표와 기능별 제공자 선택에 바로 나온다.
틀린 정의는 거절하되 적은 값을 되돌려 채운다. 쓰는 기능이 있으면 지우지 못한다. 연결 테스트는
실제 호출 결과를 적는다. 저장한 키 전체는 어디에도 다시 나오지 않는다.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import settings as settings_api
from app.config import get_settings
from app.llm import openai_compat
from app.llm import settings as store
from app.main import app
from tests.test_llm_custom import CHECK_ANSWER, FakeClient, connection_error

FULL_KEY = "sk-화면에서-넣은-딥시크-키-abcd"


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[sqlite3.Connection]:
    monkeypatch.setenv("GEMINI_API_KEY", "sk-환경변수-키-1111")
    monkeypatch.setenv("CLASSIFY_PROVIDER", "gemini")
    get_settings.cache_clear()

    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    try:
        yield connection
    finally:
        connection.close()
        get_settings.cache_clear()


@pytest.fixture
def client(tmp_path: Path, conn: sqlite3.Connection) -> Iterator[TestClient]:
    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(tmp_path / "jobs.db")
        try:
            yield connection
        finally:
            connection.close()

    app.dependency_overrides[settings_api.get_connection] = request_connection
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def form(**overrides: str) -> dict[str, str]:
    values = {
        "name": "deepseek",
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/beta",
        "schema_mode": "strict_tool",
        "temperature": "",
        "max_tokens": "32768",
        "images": "on",
        "list_models": "on",
        "models_url": "https://api.deepseek.com",
        "extra_body": '{"thinking": {"type": "disabled"}}',
        "prices": '{"deepseek-flash": {"input": 0.15, "output": 0.6}}',
        **overrides,
    }
    return values


def added_with_key(client: TestClient) -> None:
    client.put("/ui/llm/custom", data=form())
    client.put("/ui/llm/key/deepseek", data={"value": FULL_KEY})


def test_새_회사를_추가하면_목록과_키_표와_제공자_선택에_나온다(client: TestClient) -> None:
    body = client.put("/ui/llm/custom", data=form()).text

    assert "정의를 저장했다" in body
    assert 'hx-put="/ui/llm/key/deepseek"' in body
    assert '<option value="deepseek"' in body
    assert 'hx-post="/ui/llm/custom/deepseek/check"' in body


def test_틀린_정의는_거절하고_적은_값을_되돌려_채운다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    body = client.put("/ui/llm/custom", data=form(extra_body="{깨짐")).text

    assert "저장하지 못했다" in body
    assert "https://api.deepseek.com/beta" in body
    assert "{깨짐" in body
    assert store.read_config(conn).customs == ()


def test_코드에_든_제공자_이름으로는_추가하지_못한다(client: TestClient) -> None:
    body = client.put("/ui/llm/custom", data=form(name="gemini")).text

    assert "저장하지 못했다" in body
    assert "코드에 들어 있는 제공자 이름" in body


def test_추가한_회사를_기능에_지정한다(client: TestClient, conn: sqlite3.Connection) -> None:
    added_with_key(client)

    body = client.put(
        "/ui/llm/feature/classify", data={"provider": "deepseek", "model": "deepseek-flash"}
    ).text

    assert "본문 분류: deepseek 의 deepseek-flash 로 저장했다" in body
    resolved = store.settings_for(conn, "classify")
    assert resolved.classify_provider == "deepseek"
    assert resolved.llm_custom_models["deepseek"] == "deepseek-flash"


def test_쓰는_기능이_있으면_지우기를_거절한다(client: TestClient, conn: sqlite3.Connection) -> None:
    added_with_key(client)
    client.put("/ui/llm/feature/classify", data={"provider": "deepseek", "model": "deepseek-flash"})

    body = client.delete("/ui/llm/custom/deepseek").text

    assert "쓰고 있다" in body
    assert [item.name for item in store.read_config(conn).customs] == ["deepseek"]


def test_지우면_목록에서_사라진다(client: TestClient, conn: sqlite3.Connection) -> None:
    added_with_key(client)

    body = client.delete("/ui/llm/custom/deepseek").text

    assert "정의와 키를 지웠다" in body
    assert store.read_config(conn).customs == ()


def fake_sdk(monkeypatch: pytest.MonkeyPatch, fake: FakeClient) -> list[dict[str, Any]]:
    """SDK 클라이언트를 가짜로 바꾼다. 무엇으로 만들었는지 남긴다."""
    built: list[dict[str, Any]] = []

    def build(**kwargs: Any) -> FakeClient:
        built.append(kwargs)
        return fake

    monkeypatch.setattr(openai_compat, "AsyncOpenAI", build)
    return built


def test_연결_테스트_버튼이_실제_호출_결과를_적는다(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = fake_sdk(monkeypatch, FakeClient(content=None, tool_arguments=CHECK_ANSWER))
    added_with_key(client)

    body = client.post("/ui/llm/custom/deepseek/check", data={"model": "deepseek-flash"}).text

    assert "정해진 모양으로 답했다" in body
    assert built == [{"api_key": FULL_KEY, "base_url": "https://api.deepseek.com/beta"}]
    assert FULL_KEY not in body


def test_연결_테스트_실패는_사유를_적는다(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_sdk(monkeypatch, FakeClient(error=connection_error()))
    added_with_key(client)

    body = client.post("/ui/llm/custom/deepseek/check", data={"model": "deepseek-flash"}).text

    assert "연결 테스트 실패" in body
    assert "부르지 못했다" in body


def test_저장한_키_전체는_화면에_다시_나오지_않는다(client: TestClient) -> None:
    client.put("/ui/llm/custom", data=form())

    body = client.put("/ui/llm/key/deepseek", data={"value": FULL_KEY}).text

    assert FULL_KEY not in body
    assert "abcd" in body
