"""0031 전에 내보낸 DB 파일을 가져오면 칸 이름을 이 서버의 이름으로 옮긴다 (2026-09-14 결정).

| 확인 | 깨지면 |
|---|---|
| 셀렉터와 API 설정의 칸 이름을 옮긴다 | 가져온 크롤러가 새 이름의 칸을 못 찾아 빈 값만 모은다 |
| 수집 원본의 키를 옮기고 해시는 그대로다 | 같은 공고가 또 들어오거나 정규화가 칸을 못 읽는다 |
| 규칙·보정 이름을 옮기고 옛 자유 글자 직무 것은 버린다 | 규칙이 안 걸리거나 CHECK 에 걸린다 |
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from app import db
from app.api.import_data import import_database

LIST_URL = "https://example.test/jobs"
OLD_RECORD = {
    "source_url": f"{LIST_URL}/1",
    "title": "백엔드 개발자",
    "company": "예시(주)",
    "deadline": "2026-12-31",
    "body": "본문",
    "requirements": "무관",
    "duties": "서버 개발",
}


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "server.db")
    db.migrate_up(connection)
    try:
        yield connection
    finally:
        connection.close()


def old_hash(record: dict[str, str]) -> str:
    """0031 전의 해시. 키 이름이 아니라 값만 들어간다는 것을 보이려고 옛 순서로 직접 만든다."""
    joined = "\x1f".join(record[name] for name in ("source_url", "title", "deadline", "body"))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def old_upload(path: pathlib.Path) -> pathlib.Path:
    """0030 까지 올린 파일. 셀렉터·원본·규칙·보정이 전부 옛 이름이다."""
    upload = db.connect(path)
    db.migrate_up(upload)
    applied = db.applied_versions(upload)
    db.migrate_down(upload, steps=len(applied) - applied.index("0031"))
    upload.execute(
        """
        INSERT INTO crawlers (name, list_url, detail_url, selectors_json, list_mode,
                              detail_mode, api_config_json, status, default_company)
        VALUES ('예시사이트', ?, ?, ?, 'playwright', 'playwright', ?, 'promoted', '예시')
        """,
        (
            LIST_URL,
            LIST_URL + "/{id}",
            json.dumps(
                {"list": {"company": ".co"}, "detail": {"deadline": ".end", "requirements": ".req"}}
            ),
            json.dumps({"detail": {"fields": {"deadline": "data.end"}}}),
        ),
    )
    upload.execute(
        """
        INSERT INTO workflows (crawler_id, name, interval_minutes, status, auto_stop_threshold)
        VALUES (1, '예시사이트', 30, 'active', 5)
        """
    )
    upload.execute(
        """
        INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash, crawled_at)
        VALUES (1, ?, ?, ?, '2026-08-01 09:00:00')
        """,
        (
            OLD_RECORD["source_url"],
            json.dumps(OLD_RECORD, ensure_ascii=False),
            old_hash(OLD_RECORD),
        ),
    )
    upload.execute(
        """
        INSERT INTO normalization_rules (field_name, rule_type, rule_config_json, priority, enabled)
        VALUES ('company', 'mapping', ?, 0, 1), ('job_role', 'trim', '{}', 0, 1)
        """,
        (json.dumps({"map": {"예시(주)": "예시"}}, ensure_ascii=False),),
    )
    upload.execute(
        """
        INSERT INTO job_field_overrides (raw_job_id, field_name, value)
        VALUES (1, 'duties', '고친 업무'), (1, 'job_role', '자유 글자 직무')
        """
    )
    upload.commit()
    upload.close()
    return path


def test_old_file_arrives_under_the_new_names(
    conn: sqlite3.Connection, tmp_path: pathlib.Path
) -> None:
    result = import_database(conn, old_upload(tmp_path / "old.db"))

    assert result.version == "0030"
    crawler = conn.execute("SELECT selectors_json, api_config_json FROM crawlers").fetchone()
    assert json.loads(crawler["selectors_json"]) == {
        "list": {"company_name": ".co"},
        "detail": {"recruitment_end_at": ".end", "qualifications": ".req"},
    }
    assert json.loads(crawler["api_config_json"]) == {
        "detail": {"fields": {"recruitment_end_at": "data.end"}}
    }

    raw = conn.execute("SELECT raw_data_json, content_hash FROM raw_jobs").fetchone()
    assert json.loads(raw["raw_data_json"]) == {
        "source_url": OLD_RECORD["source_url"],
        "title": "백엔드 개발자",
        "company_name": "예시(주)",
        "recruitment_end_at": "2026-12-31",
        "body": "본문",
        "qualifications": "무관",
        "responsibilities": "서버 개발",
    }
    assert raw["content_hash"] == old_hash(OLD_RECORD)

    assert [row[0] for row in conn.execute("SELECT field_name FROM normalization_rules")] == [
        "company_name"
    ]
    assert result.rules_skipped == 1
    assert [
        tuple(row) for row in conn.execute("SELECT field_name, value FROM job_field_overrides")
    ] == [("responsibilities", "고친 업무")]
    assert result.overrides_skipped == 1

    job = conn.execute(
        "SELECT company_name, recruitment_end_at, qualifications, responsibilities"
        " FROM normalized_jobs"
    ).fetchone()
    assert tuple(job) == ("예시", "2026-12-31", "무관", "고친 업무")


def test_the_same_old_file_twice_is_a_duplicate(
    conn: sqlite3.Connection, tmp_path: pathlib.Path
) -> None:
    upload = old_upload(tmp_path / "old.db")
    import_database(conn, upload)

    again = import_database(conn, upload)

    assert (again.raw_added, again.raw_duplicate) == (0, 1)
