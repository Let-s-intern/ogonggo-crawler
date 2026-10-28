"""목록에서 사라져 마감으로 본 상시 채용 공고를 오공고에서 마감한다 (`app/deliver/spring.py`).

오공고를 실제로 부르지 않는다. `httpx.MockTransport` 가 오공고 관리자 API 인 척 답한다.

| 확인 | 깨지면 |
|---|---|
| 마감으로 본 공고를 오공고 id 로 마감하고 두 번 보내지 않는다 | 매일 같은 마감을 되풀이한다 |
| 직무마다 나뉜 공고는 번호마다 마감한다 | 나뉜 공고 하나만 닫히고 나머지는 모집 중이다 |
| 실패하면 사유를 남기고 세 번까지만 다시 보낸다 | 같은 거절을 끝없이 되풀이한다 |
| 오공고에 없거나 보낸 적 없는 공고는 끝난 것으로 적는다 | 마감할 것 없는 공고를 계속 고른다 |
| 마감 보내기가 꺼져 있으면 부르지 않는다 | 판정을 지켜보는 동안 오공고 공고가 닫힌다 |
| 마감으로 본 공고는 자동 전송하지 않는다 | 사이트에서 내려간 공고를 새로 등록한다 |
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator

import httpx
import pytest

from app import db
from app.config import Settings
from app.deliver import spring
from app.deliver.settings import DeliverConfig, write_config
from tests.test_deliver_spring import add_job, mock

KEY = "test-internal-key"
SETTINGS = Settings(ogonggo_internal_api_key=KEY)


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute("INSERT INTO crawlers (id, name, list_url) VALUES (1, '예시', 'https://x')")
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, '예시')")
    write_config(connection, DeliverConfig(url="https://ogonggo.test/", close_send=True))
    try:
        yield connection
    finally:
        connection.close()


def gone(conn: sqlite3.Connection, source_url: str) -> None:
    conn.execute(
        "INSERT INTO job_sightings (workflow_id, source_url, missed_runs, closed_at)"
        " VALUES (1, ?, 3, datetime('now'))",
        (source_url,),
    )


def sent(conn: sqlite3.Connection, source_url: str, job_id: int) -> None:
    conn.execute(
        "INSERT INTO spring_deliveries (source_url, spring_job_id, status) VALUES (?, ?, 'sent')",
        (source_url, job_id),
    )


def sighting(conn: sqlite3.Connection, source_url: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM job_sightings WHERE source_url = ?", (source_url,)
    ).fetchone()


async def test_마감으로_본_공고를_마감하고_두_번_보내지_않는다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://x/1"
    gone(conn, url)
    sent(conn, url, 70)
    seen = mock(lambda request: httpx.Response(200, json={"data": None}), monkeypatch)

    first = await spring.deliver_closes(conn, settings=SETTINGS)
    second = await spring.deliver_closes(conn, settings=SETTINGS)

    assert [(r.method, r.url.path) for r in seen] == [("POST", "/api/v1/internal/jobs/70/close")]
    assert seen[0].headers[spring.API_KEY_HEADER] == KEY
    assert first.closed == 1
    assert second.closed == 0
    assert sighting(conn, url)["close_sent_at"] is not None


async def test_나뉜_공고는_번호마다_마감한다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://x/1"
    gone(conn, url)
    sent(conn, url, 70)
    sent(conn, f"{url}#2", 71)
    sent(conn, "https://x/10", 99)
    seen = mock(lambda request: httpx.Response(200, json={"data": None}), monkeypatch)

    result = await spring.deliver_closes(conn, settings=SETTINGS)

    assert [r.url.path for r in seen] == [
        "/api/v1/internal/jobs/70/close",
        "/api/v1/internal/jobs/71/close",
    ]
    assert result.closed == 1


async def test_실패하면_사유를_남기고_세_번까지만_다시_보낸다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://x/1"
    gone(conn, url)
    sent(conn, url, 70)
    seen = mock(lambda request: httpx.Response(500, json={"message": "잠시 뒤"}), monkeypatch)

    for _ in range(spring.MAX_ATTEMPTS + 1):
        await spring.deliver_closes(conn, settings=SETTINGS)

    row = sighting(conn, url)
    assert len(seen) == spring.MAX_ATTEMPTS
    assert row["close_attempts"] == spring.MAX_ATTEMPTS
    assert row["close_sent_at"] is None
    assert "500" in row["close_error"]
    assert spring.close_overview(conn).failed == 1


async def test_오공고에_없거나_보낸_적_없는_공고는_끝난_것으로_적는다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    deleted, never = "https://x/1", "https://x/2"
    gone(conn, deleted)
    gone(conn, never)
    sent(conn, deleted, 70)
    seen = mock(lambda request: httpx.Response(404, json={"message": "없음"}), monkeypatch)

    result = await spring.deliver_closes(conn, settings=SETTINGS)
    again = await spring.deliver_closes(conn, settings=SETTINGS)

    assert (result.missing, result.unsent, result.failed) == (1, 1, 0)
    assert len(seen) == 1
    assert again.missing == again.unsent == 0
    assert sighting(conn, never)["close_sent_at"] is not None


async def test_마감_보내기가_꺼져_있으면_부르지_않는다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://x/1"
    gone(conn, url)
    sent(conn, url, 70)
    write_config(conn, DeliverConfig(url="https://ogonggo.test/"))
    seen = mock(lambda request: httpx.Response(200), monkeypatch)

    result = await spring.deliver_closes(conn, settings=SETTINGS)

    assert result.reason == "마감 보내기가 꺼져 있다"
    assert seen == []
    assert sighting(conn, url)["close_sent_at"] is None


def test_주소_없이_마감_보내기를_켤_수_없다(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="마감 보내기"):
        write_config(conn, DeliverConfig(close_send=True))


def test_마감으로_본_공고는_자동_전송하지_않는다(conn: sqlite3.Connection) -> None:
    kept = add_job(conn, 1)
    dropped = add_job(conn, 2)
    gone(conn, dropped)

    rows = spring.pending(conn, 10, "2026-10-28 00:00:00")

    assert [row["source_url"] for row in rows] == [kept]
