"""옛 DB 파일을 가져오면 판정 값과 날짜 규칙을 새 모양으로 옮긴다 (2026-09-14 결정).

0033 전에 내보낸 파일이 대상이다.

마이그레이션 쪽 확인은 `tests/test_spring_job_values_migration.py` 에 있다. 여기서는 옛 파일이 같은
모양으로 들어오는지만 본다.

| 확인 | 깨지면 |
|---|---|
| 보정의 한글 판정 값이 enum 이름으로 들어온다 | 사람이 고친 한글 값으로 오공고에 보낼 수 없다 |
| 날짜 규칙이 시각까지 쓰고 시각을 떼던 규칙은 꺼진다 | 마감 시각이 사라진다 |
| 시작일 규칙이 없는 파일이면 마감일 규칙을 시작일에도 건다 | 시작일을 날짜로 못 읽는다 |
| 같은 파일을 다시 올리면 복사한 규칙까지 중복이다 | 올릴 때마다 시작일 규칙이 쌓인다 |
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from app import db, field_values
from app.api.import_data import import_database

LIST_URL = "https://example.test/jobs"
RECORD = {
    "source_url": f"{LIST_URL}/1",
    "title": "백엔드 개발자",
    "company_name": "예시",
    "recruitment_end_at": "2026-08-15 09:00 ~ 2026-08-30 17:00",
    "body": "본문",
}
END = "recruitment_end_at"
OLD_RULES = [
    (END, "trim", json.dumps({"collapse_whitespace": True}), 0),
    (END, "regex", json.dumps({"pattern": "^.*?[~〜]\\s*", "replacement": ""}), 10),
    (END, "regex", json.dumps({"pattern": field_values.TIME_STRIP_PATTERN, "replacement": ""}), 20),
    (END, "date_parse", json.dumps({"formats": ["%Y-%m-%d"], "output_format": "%Y-%m-%d"}), 50),
]


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "server.db")
    db.migrate_up(connection)
    try:
        yield connection
    finally:
        connection.close()


def old_upload(path: pathlib.Path) -> pathlib.Path:
    """0032 까지 올린 파일. 보정은 한글 값이고 마감일 규칙은 시각을 뗀다."""
    upload = db.connect(path)
    db.migrate_up(upload)
    applied = db.applied_versions(upload)
    db.migrate_down(upload, steps=len(applied) - applied.index("0033"))
    upload.execute(
        """
        INSERT INTO crawlers (name, list_url, status, default_company)
        VALUES ('예시사이트', ?, 'promoted', '예시')
        """,
        (LIST_URL,),
    )
    upload.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '예시사이트')")
    upload.execute(
        """
        INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash, crawled_at)
        VALUES (1, ?, ?, 'hash-1', '2026-08-01 09:00:00')
        """,
        (RECORD["source_url"], json.dumps(RECORD, ensure_ascii=False)),
    )
    upload.executemany(
        """
        INSERT INTO normalization_rules (field_name, rule_type, rule_config_json, priority, enabled)
        VALUES (?, ?, ?, ?, 1)
        """,
        OLD_RULES,
    )
    upload.execute(
        """
        INSERT INTO job_field_overrides (raw_job_id, field_name, value)
        VALUES (1, 'employment_type', '인턴'), (1, 'education_level', '석사')
        """
    )
    upload.commit()
    upload.close()
    return path


def test_old_file_arrives_with_the_new_values(
    conn: sqlite3.Connection, tmp_path: pathlib.Path
) -> None:
    result = import_database(conn, old_upload(tmp_path / "old.db"))

    assert result.version == "0032"
    assert [
        tuple(row) for row in conn.execute("SELECT field_name, value FROM job_field_overrides")
    ] == [("employment_type", "INTERN"), ("education_level", "MASTER")]

    rules = conn.execute(
        "SELECT field_name, rule_type, rule_config_json, enabled FROM normalization_rules"
        " ORDER BY field_name, priority"
    ).fetchall()
    by_field: dict[str, list[tuple[str, str, int]]] = {}
    for row in rules:
        by_field.setdefault(str(row["field_name"]), []).append(
            (str(row["rule_type"]), str(row["rule_config_json"]), int(row["enabled"]))
        )
    # 시작일 규칙이 없던 파일이라 마감일 규칙이 시작일에도 걸린다
    assert by_field["recruitment_start_at"] == by_field[END]
    assert [enabled for _, _, enabled in by_field[END]] == [1, 1, 0, 1]
    assert json.loads(by_field[END][3][1])["output_format"] == field_values.DATETIME_OUTPUT
    assert result.rules_added == len(OLD_RULES) * 2

    job = conn.execute(
        """
        SELECT employment_type, education_level, recruitment_start_at, recruitment_end_at,
               recruitment_type
          FROM normalized_jobs
        """
    ).fetchone()
    assert tuple(job) == (
        "INTERN",
        "MASTER",
        "2026-08-15 09:00:00",
        "2026-08-30 17:00:00",
        "PERIOD",
    )


def test_the_same_old_file_twice_does_not_pile_up_start_rules(
    conn: sqlite3.Connection, tmp_path: pathlib.Path
) -> None:
    upload = old_upload(tmp_path / "old.db")
    import_database(conn, upload)

    again = import_database(conn, upload)

    assert (again.rules_added, again.rules_skipped) == (0, len(OLD_RULES))
    assert (
        conn.execute("SELECT count(*) FROM normalization_rules").fetchone()[0] == len(OLD_RULES) * 2
    )
