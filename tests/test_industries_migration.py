"""0034 산업 분류표 마이그레이션 (2026-09-14 결정).

| 확인 | 깨지면 |
|---|---|
| 올리면 표와 칸이 생기고 보정·제안이 산업을 받는다 | 산업을 고쳐 저장하면 CHECK 에 걸린다 |
| 내리면 산업 보정·제안과 칸·표만 지우고 다른 보정은 남는다 | 사람이 고친 값이 사라진다 |
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from app import db


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute("INSERT INTO crawlers (name, list_url) VALUES ('예시', 'https://x')")
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '예시')")
    connection.execute(
        "INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)"
        " VALUES (1, 1, 'https://x/1', '{}', 'h1')"
    )
    try:
        yield connection
    finally:
        connection.close()


def test_올리면_보정과_제안이_산업을_받는다(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO job_field_overrides (raw_job_id, field_name, value)"
        " VALUES (1, 'industry', '금융·은행업'), (1, 'title', '고친 제목')"
    )
    conn.execute(
        "INSERT INTO job_field_suggestions (raw_job_id, field_name, value)"
        " VALUES (1, 'industry', 'IT·정보통신업')"
    )

    assert "industries" in _tables(conn)
    assert "industry" in _columns(conn, "normalized_jobs")
    assert "industry" in _columns(conn, "job_classifications")


def test_내리면_산업만_지우고_다른_보정은_남는다(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO job_field_overrides (raw_job_id, field_name, value)"
        " VALUES (1, 'industry', '금융·은행업'), (1, 'title', '고친 제목')"
    )
    conn.execute(
        "INSERT INTO job_field_suggestions (raw_job_id, field_name, value)"
        " VALUES (1, 'industry', 'IT·정보통신업')"
    )
    applied = db.applied_versions(conn)

    db.migrate_down(conn, steps=len(applied) - applied.index("0034"))

    overrides = conn.execute("SELECT field_name, value FROM job_field_overrides").fetchall()
    assert [tuple(row) for row in overrides] == [("title", "고친 제목")]
    assert conn.execute("SELECT count(*) FROM job_field_suggestions").fetchone()[0] == 0
    assert "industries" not in _tables(conn)
    assert "industry" not in _columns(conn, "normalized_jobs")
    assert "industry" not in _columns(conn, "job_classifications")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO job_field_overrides (raw_job_id, field_name, value)"
            " VALUES (1, 'industry', '값')"
        )
