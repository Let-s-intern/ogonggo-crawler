"""렛츠커리어 콘텐츠 태그 — 서버에서 대상 받기, 태그, 보내기 (2026-10-09, LC-3448).

AI 와 오공고 서버를 부르지 않는다. 제공자는 가짜로, 서버는 `httpx.MockTransport` 로 바꿔 끼운다.

| 확인 | 깨지면 |
|---|---|
| 받은 콘텐츠에 태그를 붙여 해시와 함께 보낸다 | 오공고가 추천할 콘텐츠가 없다 |
| 목록 밖 이름은 버리고 나머지를 보낸다 | 오공고가 400 으로 통째로 거절한다 |
| 준비 단계 목록이 오공고 enum 과 같다 | 오공고가 400 으로 거절한다 |
| AI 가 연달아 실패하면 멈춘다 | 제공자가 죽은 날 콘텐츠마다 실패가 쌓인다 |
"""

from __future__ import annotations

import json
import pathlib
import re
import sqlite3
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from app import db
from app.config import Settings
from app.deliver import settings as deliver_store
from app.job_analysis import ogonggo as job_analysis_ogonggo
from app.letscareer_tags import runner, tagger
from app.llm.base import LlmCallError
from tests.test_job_analysis import FakeProvider

SERVER_TOPICS = pathlib.Path(__file__).resolve().parents[2] / (
    "ogonggo-server/ogonggo-core/src/main/kotlin/com/ogonggo/core/letscareercontent/domain/LetsCareerContentTopic.kt"
)


def target(content_id: int, content_hash: str = "h") -> dict[str, Any]:
    return {
        "contentId": content_id,
        "kind": "MATERIAL",
        "category": "MATERIAL",
        "title": "마케팅 취준 총정리 자료집",
        "description": "마케터 지원자를 위한 직무 정리",
        "labels": ["마케팅"],
        "contentHash": content_hash,
    }


class FakeOgonggo:
    def __init__(self, targets: list[dict[str, Any]]) -> None:
        self.targets = targets
        self.puts: list[tuple[int, dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Internal-Api-Key"] == "key"
        if request.method == "GET":
            assert request.url.path == "/api/v1/internal/lets-career-contents/tag-targets"
            return httpx.Response(200, json={"status": 200, "message": "ok", "data": self.targets})
        self.puts.append((int(request.url.path.split("/")[-2]), json.loads(request.content)))
        return httpx.Response(200, json={})


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    deliver_store.write_config(connection, deliver_store.DeliverConfig(url="https://ogonggo.test"))
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def settings() -> Settings:
    return Settings(ogonggo_internal_api_key="key")


def serve(monkeypatch: pytest.MonkeyPatch, server: FakeOgonggo) -> None:
    monkeypatch.setattr(job_analysis_ogonggo, "transport", httpx.MockTransport(server.handler))


def use(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider) -> None:
    monkeypatch.setattr(tagger, "for_feature", lambda feature, settings: (provider, "fake-model"))


async def test_태그를_붙여_해시와_함께_보내고_목록_밖_이름은_버린다(
    conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FakeOgonggo([target(3, "h3")])
    serve(monkeypatch, server)
    provider = FakeProvider(
        {
            "job_fields": ["MARKETING_ADVERTISING", "마케팅"],
            "job_roles": [],
            "topics": ["CAREER_START", "NOT_A_TOPIC"],
        }
    )
    use(monkeypatch, provider)

    summary = await runner.run_once(conn, settings=settings)

    assert (summary.status, summary.targets, summary.sent) == ("success", 1, 1)
    assert server.puts == [
        (
            3,
            {
                "contentHash": "h3",
                "jobFields": ["MARKETING_ADVERTISING"],
                "jobRoles": [],
                "topics": ["CAREER_START"],
            },
        )
    ]
    assert "마케팅 취준 총정리 자료집" in provider.prompts[0]
    assert "MARKETING_ADVERTISING: 마케팅·광고" in provider.prompts[0]
    calls = conn.execute("SELECT feature FROM llm_calls").fetchall()
    assert [row["feature"] for row in calls] == ["letscareer_tags"]


async def test_AI_가_연달아_실패하면_멈춘다(
    conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, FakeOgonggo([target(i) for i in range(1, 8)]))
    use(monkeypatch, FakeProvider(*[LlmCallError("timeout", "느림") for _ in range(5)]))

    summary = await runner.run_once(conn, settings=settings)

    assert (summary.status, summary.failed, summary.left) == ("failed", 5, 2)


def test_준비_단계_목록이_오공고_enum_과_같다() -> None:
    if not SERVER_TOPICS.exists():
        pytest.skip("오공고 서버 저장소가 옆에 없다")
    source = SERVER_TOPICS.read_text(encoding="utf-8")
    names = re.findall(r"^\s+([A-Z_]+)\(\d+,", source, flags=re.MULTILINE)
    assert names == list(tagger.TOPICS)
