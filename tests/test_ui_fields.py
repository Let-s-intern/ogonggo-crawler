"""설정 > 수집 항목 화면과 AI 빠른 설정 (2026-09-17, LC-3344).

AI 를 부르지 않는다. 미리 보기·연결은 가짜로 바꿔 끼운다.

| 확인 | 깨지면 |
|---|---|
| 오공고로 보내는 칸이 출처·오공고 칸 이름과 함께 나온다 | 어떤 칸이 어디서 오는지 모른다 |
| 항목을 더하고, 끄고, 지운다 | 화면에서 항목을 관리할 수 없다 |
| 미리 보기는 저장하지 않는다 | 보기만 했는데 항목이 생긴다 |
| 다시 채우기는 시작만 하고 진행을 보여 준다 | 요청이 수백 건 호출을 기다리다 끊긴다 |
| DeepSeek 키 하나로 분류·셀렉터 생성·수정·이미지 읽기를 지정한다 | 키를 넣고도 다른 AI 로 돈다 |
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import custom_fields, db
from app.api import crawlers as crawlers_api
from app.api import settings as settings_api
from app.api import ui_fields
from app.llm import settings as llm_store
from app.main import app


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute("INSERT INTO crawlers (id, name, list_url) VALUES (1, 'x', 'https://x')")
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, 'x')")
    for seq in (1, 2):
        connection.execute(
            "INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)"
            " VALUES (?, 1, ?, '{}', ?)",
            (seq, f"https://x/{seq}", f"h{seq}"),
        )
        connection.execute(
            "INSERT INTO normalized_jobs (raw_job_id, source_url, title, body, region)"
            " VALUES (?, ?, ?, '하이브리드 근무', ?)",
            (seq, f"https://x/{seq}", f"공고 {seq}", "서울" if seq == 1 else None),
        )
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def client(tmp_path: pathlib.Path, conn: sqlite3.Connection) -> Iterator[TestClient]:
    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(tmp_path / "jobs.db")
        try:
            yield connection
        finally:
            connection.close()

    app.dependency_overrides[crawlers_api.get_connection] = request_connection
    app.dependency_overrides[settings_api.get_connection] = request_connection
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_수집_항목_화면이_목록을_부르고_보내는_칸을_보인다(client: TestClient) -> None:
    assert 'hx-get="/ui/fields"' in client.get("/settings/fields").text

    html = client.get("/ui/fields").text

    assert "오공고로 보내는 칸" in html
    assert "recruitmentEndAt" in html and "jobField" in html
    region = html[html.index(">근무 지역<") :]
    assert "50%" in region[: region.index("</tr>")]


def test_항목을_더하고_끄고_지운다(client: TestClient, conn: sqlite3.Connection) -> None:
    added = client.post(
        "/ui/fields",
        data={
            "label": "근무 형태",
            "instruction": "재택 여부",
            "value_type": "choice",
            "choices": "재택\n출근",
        },
    )
    assert "항목을 더했습니다" in added.text
    assert added.headers["HX-Trigger"] == "fields-changed"
    field_id = custom_fields.list_fields(conn)[0].id

    assert "근무 형태" in client.get("/ui/fields").text
    toggled = client.post(f"/ui/fields/{field_id}/toggle").text
    assert "껐다" in toggled
    assert custom_fields.list_fields(conn, enabled_only=True) == []

    removed = client.post(f"/ui/fields/{field_id}/delete").text
    assert "지웠다" in removed
    assert custom_fields.list_fields(conn) == []


def test_잘못된_항목은_창에_사유를_적는다(client: TestClient, conn: sqlite3.Connection) -> None:
    html = client.post("/ui/fields", data={"label": "", "value_type": "text"}).text

    assert "이름이 비었다" in html
    assert custom_fields.list_fields(conn) == []


def test_미리_보기는_저장하지_않는다(
    client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_extract(
        conn: Any, fields: Any, title: str, body: str, **_: Any
    ) -> dict[int, str]:
        return {0: f"{title} 값"}

    monkeypatch.setattr(custom_fields, "extract", fake_extract)

    html = client.post("/ui/fields/preview", data={"label": "근무 형태", "value_type": "text"}).text

    assert "공고 2 값" in html and "공고 1 값" in html
    assert "저장하지 않았습니다" in html
    assert custom_fields.list_fields(conn) == []


def test_다시_채우기는_시작만_하고_진행을_보여_준다(
    client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    field = custom_fields.add_field(conn, "근무 형태", "", "text")
    launched: list[Any] = []
    app.dependency_overrides[ui_fields.get_fill_launcher] = lambda: launched.append
    monkeypatch.setattr(ui_fields, "_progress", ui_fields.FillProgress())

    html = client.post(f"/ui/fields/{field.id}/fill", data={"limit": "20"}).text

    assert "채우기 시작했다" in html
    assert 'hx-trigger="every 2s"' in html
    assert len(launched) == 1
    launched[0].close()


def test_DeepSeek_키가_비면_저장하지_않는다(client: TestClient) -> None:
    assert 'hx-get="/ui/llm/quick"' in client.get("/settings").text
    assert "API 키가 비었습니다" in client.post("/ui/llm/quick", data={"api_key": " "}).text


def test_DeepSeek_키_하나로_분류_셀렉터_이미지_읽기를_지정한다(
    client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_models(conn: Any, provider: str, settings: Any = None) -> tuple[list[str], str]:
        return ["deepseek-v4-pro", "deepseek-flash"], ""

    monkeypatch.setattr(llm_store, "list_models", fake_models)

    html = client.post("/ui/llm/quick", data={"api_key": "sk-test-1234"}).text

    assert "deepseek-flash 로 돕니다" in html
    assert "sk-test-1234" not in html
    features = {
        view.feature: (view.provider, view.model) for view in llm_store.read_config(conn).features
    }
    for feature in ("classify", "selector_generate", "selector_repair", "image_read"):
        assert features[feature] == ("deepseek", "deepseek-flash")


def test_DeepSeek_에_연결하지_못하면_기능을_바꾸지_않는다(
    client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing(conn: Any, provider: str, settings: Any = None) -> tuple[list[str], str]:
        return [], "401 인증 실패"

    monkeypatch.setattr(llm_store, "list_models", failing)

    html = client.post("/ui/llm/quick", data={"api_key": "sk-wrong"}).text

    assert "연결하지 못했습니다: 401 인증 실패" in html
    providers = {view.feature: view.provider for view in llm_store.read_config(conn).features}
    assert providers["classify"] != "deepseek"
