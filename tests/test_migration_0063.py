"""0063 이 쌓인 롯데 주소에서 목록 위치를 뗀다 (LC-3504, 2026-10-23).

실제 `migrations/` 를 0062 까지 적용한 DB 에 같은 공고를 목록 위치만 다른 주소로 두 번 넣고 0063 을
올린다.

| 확인 | 깨지면 |
|---|---|
| 원본·정규화 주소에서 위치가 떨어지고 나눈 번호는 남는다 | 다음 수집이 새 공고로 읽는다 |
| 겹친 전송 기록은 보낸 행 하나만 남는다 | 보낸 공고가 안 보낸 공고로 읽혀 다시 간다 |
| 다른 사이트 주소는 그대로다 | 쌓인 공고가 전부 새 공고가 된다 |
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from app import db

LOTTE = "https://recruit.lotte.co.kr/apply/announcement/detail/21995366"
FIRST = f"{LOTTE}?compcd=?id=bookmark1001"
LATER = f"{LOTTE}?compcd=?id=bookmark1004#1"
OTHER = "https://job.alio.go.kr/recruitview.do?idx=306026#2"


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "lotte.db")
    db.migrate_up(connection)
    applied = db.applied_versions(connection)
    db.migrate_down(connection, steps=len(applied) - applied.index("0063"))
    assert db.applied_versions(connection)[-1] == "0062"
    yield connection
    connection.close()


def _seed(connection: sqlite3.Connection) -> None:
    connection.execute("INSERT INTO crawlers (name, list_url) VALUES ('롯데', 'https://x')")
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '롯데')")
    for index, url in enumerate((FIRST, LATER.removesuffix("#1"), OTHER), start=1):
        connection.execute(
            "INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)"
            " VALUES (?, 1, ?, '{}', ?)",
            (index, url, f"h{index}"),
        )
    for index, url in enumerate((FIRST, LATER, OTHER), start=1):
        connection.execute(
            "INSERT INTO normalized_jobs (raw_job_id, source_url) VALUES (?, ?)", (index, url)
        )
    connection.executemany(
        "INSERT INTO spring_deliveries (source_url, spring_job_id, status, attempts, last_error)"
        " VALUES (?, ?, ?, 1, '')",
        [
            (f"{LOTTE}?compcd=?id=bookmark1000", None, "failed"),
            (FIRST, 17778, "sent"),
            (OTHER, 17640, "sent"),
        ],
    )
    connection.execute(
        "INSERT INTO discarded_jobs (workflow_id, source_url) VALUES (1, ?), (1, ?)",
        (FIRST, LATER.removesuffix("#1")),
    )
    connection.commit()


def _urls(connection: sqlite3.Connection, table: str) -> list[str]:
    rows = connection.execute(f"SELECT source_url FROM {table} ORDER BY rowid").fetchall()
    return [str(row[0]) for row in rows]


def test_the_list_position_comes_off_stored_urls(conn: sqlite3.Connection) -> None:
    _seed(conn)

    db.migrate_up(conn)

    assert _urls(conn, "raw_jobs") == [LOTTE, LOTTE, OTHER]
    assert _urls(conn, "normalized_jobs") == [LOTTE, f"{LOTTE}#1", OTHER]
    assert _urls(conn, "discarded_jobs") == [LOTTE]


def test_the_sent_delivery_survives_the_merge(conn: sqlite3.Connection) -> None:
    _seed(conn)

    db.migrate_up(conn)

    rows = conn.execute(
        "SELECT source_url, spring_job_id, status FROM spring_deliveries ORDER BY source_url"
    ).fetchall()
    assert [tuple(row) for row in rows] == [(OTHER, 17640, "sent"), (LOTTE, 17778, "sent")]


def test_an_empty_database_migrates(conn: sqlite3.Connection) -> None:
    db.migrate_up(conn)

    assert "0063" in db.applied_versions(conn)
