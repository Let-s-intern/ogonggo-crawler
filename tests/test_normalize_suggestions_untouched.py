"""정규화는 `job_field_suggestions` 를 읽지 않는다 (11.5.V).

제안은 값이 있는 칸(`company_name`·`recruitment_end_at`·`recruitment_start_at`)을 자동으로 덮지
않는다 — 사람이 검수 화면에서 수락해야 `job_field_overrides` 로 옮겨 간다
(`.claude/tasks/todo/prd-side-workflows.md` 6절). 이 표를 `app/normalize/engine.py` 의 어느 경로가
읽기 시작하면 그 경계가 조용히 사라지므로, 여기서는 동작과 소스 둘 다로 못박는다.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from app import db
from app.normalize.engine import insert_normalized, load_rules, normalized_values


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        """
        INSERT INTO crawlers (name, list_url, default_company)
        VALUES ('테스트', 'https://x', '테스트')
        """
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '테스트')")
    connection.execute(
        """
        INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash)
        VALUES (1, 'https://x/1', ?, 'hash1')
        """,
        (
            json.dumps(
                {
                    "source_url": "https://x/1",
                    "title": "마케팅 기획",
                    "body": "본문",
                    "company_name": "한화생명",
                    "recruitment_end_at": "2026-08-31",
                },
                ensure_ascii=False,
            ),
        ),
    )
    try:
        yield connection
    finally:
        connection.close()


def _add_suggestion(conn: sqlite3.Connection, field_name: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO job_field_suggestions (raw_job_id, field_name, value, reason)
        VALUES (1, ?, ?, '원문과 다르다')
        """,
        (field_name, value),
    )


def test_a_suggestion_does_not_change_what_normalized_values_computes(
    conn: sqlite3.Connection,
) -> None:
    """제안이 있는 건을 재정규화해도 확정 값이 그대로다."""
    rules = load_rules(conn)
    before = normalized_values(conn, 1, rules)

    _add_suggestion(conn, "company_name", "한화솔루션")
    _add_suggestion(conn, "recruitment_end_at", "2026-09-30")

    after = normalized_values(conn, 1, rules)
    assert after == before
    assert after[1]["company_name"] == "한화생명"
    assert after[1]["recruitment_end_at"] == "2026-08-31"


def test_a_suggestion_does_not_change_the_inserted_row(conn: sqlite3.Connection) -> None:
    """`insert_normalized` 가 실제로 쓰는 값도 제안과 무관하다."""
    rules = load_rules(conn)
    _add_suggestion(conn, "company_name", "한화솔루션")

    row_id = insert_normalized(conn, 1, rules)

    row = conn.execute(
        "SELECT company_name, recruitment_end_at FROM normalized_jobs WHERE id = ?", (row_id,)
    ).fetchone()
    assert (row["company_name"], row["recruitment_end_at"]) == ("한화생명", "2026-08-31")


def test_engine_source_never_mentions_the_suggestions_table() -> None:
    """정규화의 어느 경로도 이 표 이름을 알지 못한다. 코드 자체로 못박는다."""
    import app.normalize.engine as engine

    source = pathlib.Path(engine.__file__).read_text(encoding="utf-8")
    assert "job_field_suggestions" not in source
