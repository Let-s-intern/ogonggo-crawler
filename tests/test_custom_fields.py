"""운영자가 더한 수집 항목 — 정의, AI 로 채우기, 공고와 함께 지우기 (2026-09-17, LC-3344).

AI 를 부르지 않는다. 제공자를 가짜로 바꿔 끼우고, 무엇을 묻고 받은 값을 어떻게 다듬어 남기는지 본다.

| 확인 | 깨지면 |
|---|---|
| 이름·형식·보기를 검사한다 | 빈 이름이나 보기 없는 `보기 중 하나` 가 생긴다 |
| 켜진 항목만 한 번에 묻고, 형식에 안 맞는 값은 버린다 | 목록 밖 값이 남는다 |
| 항목이 없으면 부르지 않는다 | 분류마다 헛호출이 나간다 |
| 부르다 실패해도 예외를 올리지 않는다 | 추가 항목 때문에 분류가 실패로 보인다 |
| 공고를 지우면 그 공고의 값도 지운다 | 외래키로 공고가 안 지워진다 |
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from app import custom_fields, db
from app.api import review_filter
from app.llm.base import LlmCallError, Usage


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute("INSERT INTO crawlers (id, name, list_url) VALUES (1, 'x', 'https://x')")
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, 'x')")
    connection.execute(
        "INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)"
        " VALUES (1, 1, 'https://x/1', '{}', 'h1')"
    )
    connection.execute(
        "INSERT INTO normalized_jobs (raw_job_id, source_url, title, body)"
        " VALUES (1, 'https://x/1', '백엔드 개발자', '근무 형태: 주 2회 재택 하이브리드')"
    )
    try:
        yield connection
    finally:
        connection.close()


class FakeProvider:
    def __init__(self, answer: dict[str, Any] | Exception) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def build_client(self, settings: Any) -> object:
        return object()

    async def call_model(
        self, client: Any, model: str, prompt: str, *args: Any, **kwargs: Any
    ) -> tuple[str, Usage]:
        self.prompts.append(prompt)
        if isinstance(self.answer, Exception):
            raise self.answer
        usage = Usage(
            provider="fake",
            model=model,
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            latency_ms=1,
        )
        return json.dumps(self.answer, ensure_ascii=False), usage


def use(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider) -> None:
    monkeypatch.setattr(
        custom_fields, "for_feature", lambda feature, settings: (provider, "fake-model")
    )


def test_이름과_형식과_보기를_검사한다(conn: sqlite3.Connection) -> None:
    with pytest.raises(custom_fields.CustomFieldError, match="이름이 비었다"):
        custom_fields.add_field(conn, " ", "", "text")
    with pytest.raises(custom_fields.CustomFieldError, match="두 개 이상"):
        custom_fields.add_field(conn, "근무 형태", "", "choice", "재택")
    added = custom_fields.add_field(
        conn, "근무 형태", "재택 여부", "choice", "재택\n하이브리드\n\n출근"
    )
    assert added.choices == ("재택", "하이브리드", "출근")
    with pytest.raises(custom_fields.CustomFieldError, match="이미 있다"):
        custom_fields.add_field(conn, "근무 형태", "", "text")


def test_켜진_항목만_묻고_형식에_안_맞는_값은_버린다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    work = custom_fields.add_field(
        conn, "근무 형태", "재택 여부", "choice", "재택\n하이브리드\n출근"
    )
    days = custom_fields.add_field(conn, "재택 일수", "", "number")
    off = custom_fields.add_field(conn, "꺼진 항목", "", "text")
    custom_fields.set_enabled(conn, off.id, False)
    provider = FakeProvider({work.key: "하이브리드", days.key: "주 2회"})
    use(monkeypatch, provider)

    assert asyncio_run(custom_fields.fill_job(conn, 1)) == 1

    values = dict((item.label, value) for item, value in custom_fields.values_for(conn, 1, 1))
    assert values == {"근무 형태": "하이브리드", "재택 일수": ""}
    assert "꺼진 항목" not in provider.prompts[0]
    assert "보기: 재택, 하이브리드, 출근" in provider.prompts[0]
    calls = conn.execute("SELECT feature, model FROM llm_calls").fetchall()
    assert [(row["feature"], row["model"]) for row in calls] == [("classify", "fake-model")]


def test_항목이_없으면_부르지_않는다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider({})
    use(monkeypatch, provider)

    assert asyncio_run(custom_fields.fill_job(conn, 1)) == 0
    assert provider.prompts == []


def test_부르다_실패해도_예외를_올리지_않는다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom_fields.add_field(conn, "근무 형태", "", "text")
    use(monkeypatch, FakeProvider(LlmCallError("api_error", "503")))

    assert asyncio_run(custom_fields.fill_job(conn, 1)) == 0


def test_공고를_지우면_그_공고의_값도_지운다(conn: sqlite3.Connection) -> None:
    added = custom_fields.add_field(conn, "근무 형태", "", "text")
    custom_fields.save_values(conn, 1, 1, {added.id: "재택"})

    review_filter._delete_rows(conn, (1,))

    assert conn.execute("SELECT count(*) AS n FROM job_custom_values").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) AS n FROM raw_jobs").fetchone()["n"] == 0


def asyncio_run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)
