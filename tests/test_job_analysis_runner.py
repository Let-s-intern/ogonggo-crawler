"""매일 공고 분석 — 서버에서 대상 받기, 분석, 보내기, 기록 (2026-10-08, LC-3446).

AI 와 오공고 서버를 부르지 않는다. 제공자는 가짜로, 서버는 `httpx.MockTransport` 로 바꿔 끼운다.

| 확인 | 깨지면 |
|---|---|
| 받은 공고를 분석해 해시와 판을 실어 보내고 기록한다 | 무엇을 보냈는지 화면에서 볼 수 없다 |
| 409 는 본문 바뀜, 그 밖의 거절은 못 보냄으로 남긴다 | 낡은 분석과 실패가 섞인다 |
| 분석해 두고 못 보낸 공고는 AI 없이 다시 보낸다 | 전송만 실패한 공고에 토큰을 다시 쓴다 |
| 멈춤 시각이 지나면 남은 공고를 넘긴다 | 크롤러 서버가 꺼지는 시각까지 붙잡는다 |
| AI 가 연달아 실패하면 멈춘다 | 제공자가 죽은 날 공고마다 실패가 쌓인다 |
| 오공고 주소가 없으면 실행을 실패로 닫는다 | 실행 기록이 도는 중으로 남는다 |
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app import db
from app.config import Settings
from app.deliver import settings as deliver_store
from app.job_analysis import analyzer, guide, ogonggo, runner, store
from app.llm.base import LlmCallError
from tests.test_job_analysis import FakeProvider, answer

QUALIFICATION = "모호한 요구 사항 속에서 핵심 문제를 정의하고 해결책을 도출하는 분"


def target(job_id: int, content_hash: str = "h") -> dict[str, Any]:
    return {
        "jobId": job_id,
        "source": "WORK24",
        "contentHash": content_hash,
        "title": f"공고 {job_id}",
        "companyName": "회사",
        "qualifications": QUALIFICATION,
    }


class FakeOgonggo:
    def __init__(self, targets: list[dict[str, Any]], put_status: dict[int, int] | None = None):
        self.targets = targets
        self.put_status = put_status or {}
        self.puts: list[tuple[int, dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.headers["X-Internal-Api-Key"] == "key"
            return httpx.Response(200, json={"status": 200, "message": "ok", "data": self.targets})
        job_id = int(request.url.path.split("/")[-2])
        self.puts.append((job_id, json.loads(request.content)))
        return httpx.Response(self.put_status.get(job_id, 200), json={})


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
    monkeypatch.setattr(ogonggo, "transport", httpx.MockTransport(server.handler))


def use(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider) -> None:
    monkeypatch.setattr(analyzer, "for_feature", lambda feature, settings: (provider, "fake-model"))


async def test_분석해_해시와_판을_실어_보내고_기록한다(
    conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FakeOgonggo([target(11, "h11")])
    serve(monkeypatch, server)
    use(monkeypatch, FakeProvider(answer()))
    version = guide.save(conn, guide.Guide("지시", dict(guide.DEFAULT_GUIDE.sections))).number

    summary = await runner.run_once(conn, settings=settings)

    assert (summary.status, summary.targets, summary.analyzed, summary.sent) == ("success", 1, 1, 1)
    job_id, body = server.puts[0]
    assert job_id == 11
    assert body["contentHash"] == "h11" and body["guideVersion"] == version
    assert body["analysis"]["competencies"][0]["name"] == "문제 정의"
    row = store.get(conn, 11)
    assert row is not None
    assert (row["status"], row["source"], row["guide_version"]) == ("sent", "WORK24", version)
    assert row["sent_at"] is not None
    run = conn.execute("SELECT * FROM job_analysis_runs").fetchone()
    assert (run["status"], run["sent_count"]) == ("success", 1)


async def test_409_는_본문_바뀜_그_밖의_거절은_못_보냄이다(
    conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, FakeOgonggo([target(1), target(2)], {1: 409, 2: 500}))
    use(monkeypatch, FakeProvider(answer(), answer()))

    summary = await runner.run_once(conn, settings=settings)

    assert (summary.analyzed, summary.sent, summary.failed) == (2, 0, 2)
    assert store.get(conn, 1)["status"] == "stale"  # type: ignore[index]
    assert store.get(conn, 2)["status"] == "unsent"  # type: ignore[index]


async def test_분석해_두고_못_보낸_공고는_AI_없이_다시_보낸다(
    conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, FakeOgonggo([target(1)], {1: 500}))
    use(monkeypatch, FakeProvider(answer()))
    await runner.run_once(conn, settings=settings)

    server = FakeOgonggo([target(1)])
    serve(monkeypatch, server)
    provider = FakeProvider()
    use(monkeypatch, provider)
    summary = await runner.run_once(conn, settings=settings)

    assert provider.prompts == []
    assert (summary.analyzed, summary.sent) == (0, 1)
    assert store.get(conn, 1)["status"] == "sent"  # type: ignore[index]


async def test_멈춤_시각이_지나면_남은_공고를_넘긴다(
    conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, FakeOgonggo([target(1), target(2)]))
    provider = FakeProvider()
    use(monkeypatch, provider)

    summary = await runner.run_once(
        conn, settings=settings, stop_at=datetime.now(UTC) - timedelta(minutes=1)
    )

    assert provider.prompts == []
    assert summary.left == 2
    assert "다음 실행에 넘겼다" in summary.notes[0]


async def test_AI_가_연달아_실패하면_멈춘다(
    conn: sqlite3.Connection, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    count = runner.MAX_FAILURES_IN_ROW + 2
    serve(monkeypatch, FakeOgonggo([target(n) for n in range(count)]))
    use(monkeypatch, FakeProvider(*[LlmCallError("api_error", "503")] * count))

    summary = await runner.run_once(conn, settings=settings)

    assert summary.status == "failed"
    assert (summary.failed, summary.left) == (runner.MAX_FAILURES_IN_ROW, 2)
    assert store.counts(conn)["failed"] == runner.MAX_FAILURES_IN_ROW


async def test_오공고_주소가_없으면_실행을_실패로_닫는다(
    conn: sqlite3.Connection, settings: Settings
) -> None:
    deliver_store.write_config(conn, deliver_store.DeliverConfig(url=""))

    summary = await runner.run_once(conn, settings=settings)

    assert summary.status == "failed"
    run = conn.execute("SELECT status, notes FROM job_analysis_runs").fetchone()
    assert run["status"] == "failed" and "오공고 주소" in run["notes"]
