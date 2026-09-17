"""화면에서 저장한 설정이 실제 호출에 쓰이는지 (2.9.V).

**배포 없이 다음 호출부터 적용된다**가 이 PRD 의 3번 조건이다
(`.claude/tasks/todo/prd-llm-providers.md`). 저장소만 있고 호출 자리가 그것을 읽지 않으면
화면은 값을 받아 두기만 하는 상자가 된다.

실제로 부르지는 않는다. 제공자 항목을 기록기로 갈아 끼우고 **어느 항목이 어느 모델로
불렸는지**만 본다 (`tests/test_llm_feature_provider.py` 와 같은 방식이다).
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from app import db
from app.api import crawlers as crawlers_api
from app.classify.batch import ClassifyProgress, classify_ids
from app.classify.store import pending_ids
from app.llm import settings as store
from app.llm.log import CLASSIFY, SELECTOR_GENERATE, SELECTOR_REPAIR
from tests.test_classify_run import BODY
from tests.test_llm_feature_provider import Recorder, recorders
from tests.test_llm_settings import env

__all__ = ["recorders"]

SAVED_KEY = "sk-화면에서-저장한-키-5555"


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (id, name, list_url, status) "
        "VALUES (1, '테스트', 'https://x', 'promoted')"
    )
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, '테스트')")
    raw = {"source_url": "https://x/1", "title": "공고", "body": BODY, "company_name": "테스트회사"}
    connection.execute(
        "INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash) "
        "VALUES (1, ?, ?, 'hash1')",
        (raw["source_url"], json.dumps(raw, ensure_ascii=False)),
    )
    try:
        yield connection
    finally:
        connection.close()


async def test_분류가_DB_에_저장된_제공자와_모델로_나간다(
    conn: sqlite3.Connection, recorders: dict[str, Recorder]
) -> None:
    base = env(classify_provider="gemini")
    store.write_key(conn, "qwen", SAVED_KEY, base)
    store.write_feature(conn, CLASSIFY, "qwen", "qwen3.8-flash", base)

    await classify_ids(conn, pending_ids(conn), ClassifyProgress(), settings=base)

    # 분류 호출과 회사·모집 기간 호출 둘 다 저장된 모델로 나간다
    assert {call["model"] for call in recorders["qwen"].calls} == {"qwen3.8-flash"}
    assert recorders["gemini"].calls == []


async def test_생성_의존성이_저장된_설정을_들고_내려간다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []

    async def spy(list_url: str, detail_url: str, **kwargs: Any) -> Any:
        seen.append(kwargs["settings"])
        raise AssertionError("여기까지 오면 설정은 확인됐다")

    store.write_key(conn, "claude", SAVED_KEY, env())
    store.write_feature(conn, SELECTOR_GENERATE, "claude", "claude-sonnet-5", env())
    monkeypatch.setattr(crawlers_api, "generate_for_urls", spy)
    monkeypatch.setattr(crawlers_api, "open_source", _source())
    monkeypatch.setattr(crawlers_api, "get_fetcher", lambda: None)

    generate = crawlers_api.get_generator(conn)
    with pytest.raises(AssertionError):
        await generate("https://x", "https://x/1", "static")

    assert seen[0].selector_generate_provider == "claude"
    assert seen[0].claude_model == "claude-sonnet-5"
    assert seen[0].claude_api_key == SAVED_KEY


async def test_고치기_의존성이_생성과_다른_제공자를_들고_내려간다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """생성과 고치기가 각자 고른다. 한쪽 설정이 다른 쪽을 끌고 가지 않는다."""
    seen: list[Any] = []

    async def spy(list_url: str, detail_url: str, selectors: Any, **kwargs: Any) -> Any:
        seen.append(kwargs["settings"])
        raise AssertionError("여기까지 오면 설정은 확인됐다")

    store.write_key(conn, "claude", SAVED_KEY, env())
    store.write_key(conn, "gpt", SAVED_KEY, env())
    store.write_feature(conn, SELECTOR_GENERATE, "claude", "claude-sonnet-5", env())
    store.write_feature(conn, SELECTOR_REPAIR, "gpt", "gpt-5.6-terra", env())
    monkeypatch.setattr(crawlers_api, "repair_for_urls", spy)
    monkeypatch.setattr(crawlers_api, "open_source", _source())
    monkeypatch.setattr(crawlers_api, "get_fetcher", lambda: None)

    repair = crawlers_api.get_repairer(conn)
    with pytest.raises(AssertionError):
        await repair("https://x", "https://x/1", "static", None)  # type: ignore[arg-type]

    assert seen[0].selector_repair_provider == "gpt"
    assert seen[0].gpt_model == "gpt-5.6-terra"


def _source() -> Any:
    """`open_source` 자리. 브라우저도 fetch 도 열지 않는다."""

    class Source:
        async def __aenter__(self) -> Source:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

    def open_source(render_mode: str, fetcher: Any) -> Source:
        return Source()

    return open_source
