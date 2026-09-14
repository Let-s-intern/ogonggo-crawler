"""0031 이 칸 이름을 오공고(Spring) Job 엔티티의 칼럼 이름으로 옮긴다 (2026-09-14 결정).

실제 `migrations/` 를 0030 까지 적용한 DB 에 옛 이름으로 값을 넣고 0031 을 올리고 내린다.

| 확인 | 깨지면 |
|---|---|
| 저장 표부터 수집 원본까지 새 이름으로 옮기고 값은 그대로다 | 옛 이름의 값을 못 읽어 공고가 빈다 |
| 옛 자유 글자 직무는 지우고 분류의 값은 제목용으로 남긴다 | 소분류와 자유 글자가 한 칸에 섞인다 |
| 옛 이름은 보정 CHECK 에 걸린다 | 옛 이름으로 적힌 보정이 어느 칸에도 덮이지 않고 쌓인다 |
| 깨진 JSON 행은 건드리지 않는다 | 원본 한 행 때문에 마이그레이션 전체가 실패한다 |
| 내리면 옛 이름으로 돌아간다 | 배포를 되돌릴 수 없다 |
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from app import db

SELECTORS = {
    "list": {"item": ".job", "title": ".t", "link": "a", "date": ".d", "company": ".company"},
    "detail": {"title": "h1", "body": ".b", "requirements": ".req", "deadline": ".deadline"},
}
API_CONFIG = {
    "list": {"fields": {"title": "t", "company": "companyName"}, "body": {"company": "사이트 값"}},
    "detail": {"fields": {"deadline": "data.end", "duties": "data.task"}},
}
RAW = {
    "title": "백엔드",
    "company": "예시",
    "deadline": "2026-12-31",
    "body": "본문",
    "etc_info": "안내",
}


def _down_to_0030(connection: sqlite3.Connection) -> None:
    """0031 과 그 뒤를 되돌린다. 뒤에 마이그레이션이 붙어도 걸음 수를 0031 의 자리에서 센다."""
    applied = db.applied_versions(connection)
    db.migrate_down(connection, steps=len(applied) - applied.index("0031"))


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    """0030 까지 올린 DB 에 옛 이름으로 한 벌을 넣는다."""
    connection = db.connect(tmp_path / "rename.db")
    db.migrate_up(connection)
    _down_to_0030(connection)
    assert db.applied_versions(connection)[-1] == "0030"
    connection.execute(
        """
        INSERT INTO crawlers (name, list_url, selectors_json, api_config_json)
        VALUES ('예시', 'https://example.test', ?, ?)
        """,
        (json.dumps(SELECTORS, ensure_ascii=False), json.dumps(API_CONFIG, ensure_ascii=False)),
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '예시')")
    connection.execute(
        """
        INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash)
        VALUES (1, 'https://example.test/1', ?, 'hash'), (1, 'https://example.test/2', '{끊긴', 'h')
        """,
        (json.dumps(RAW, ensure_ascii=False),),
    )
    connection.execute(
        """
        INSERT INTO normalized_jobs (raw_job_id, source_url, company, deadline, job_role,
                                     job_major, job_minor, parent_company)
        VALUES (1, 'https://example.test/1', '예시', '2026-12-31', '자유 글자 직무',
                'IT·개발', '서버·백엔드', '모회사')
        """
    )
    connection.execute(
        """
        INSERT INTO job_classifications (raw_job_id, model, job_role, job_minor, career_level,
                                         evidence_json, dropped_fields)
        VALUES (1, 'm', 'HS사업본부 기계', '서버·백엔드', '경력', ?, 'job_role, duties, job_minor')
        """,
        (json.dumps({"career_level_evidence": "5년 이상", "job_minor_evidence": "서버"}),),
    )
    for name in ("company", "job_role", "job_minor"):
        connection.execute(
            "INSERT INTO job_field_overrides (raw_job_id, field_name, value) VALUES (1, ?, ?)",
            (name, f"{name} 보정"),
        )
        connection.execute(
            "INSERT INTO job_field_suggestions (raw_job_id, field_name, value) VALUES (1, ?, ?)",
            (name, f"{name} 제안"),
        )
    connection.execute(
        """
        INSERT INTO normalization_rules (field_name, rule_type, rule_config_json)
        VALUES ('deadline', 'trim', '{}'), ('job_role', 'trim', '{}')
        """
    )
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


def test_up_moves_every_name_and_keeps_the_values(conn: sqlite3.Connection) -> None:
    db.migrate_up(conn)
    # 0031 이 올린 모습만 본다. 뒤의 0033 은 값의 모양까지 바꾼다
    # (`tests/test_spring_job_values_migration.py`)
    applied = db.applied_versions(conn)
    db.migrate_down(conn, steps=len(applied) - applied.index("0031") - 1)

    job = conn.execute("SELECT * FROM normalized_jobs").fetchone()
    assert (job["company_name"], job["recruitment_end_at"], job["parent_company_name"]) == (
        "예시",
        "2026-12-31",
        "모회사",
    )
    assert (job["job_field"], job["job_role"]) == ("IT·개발", "서버·백엔드")

    classified = conn.execute("SELECT * FROM job_classifications").fetchone()
    assert classified["position_name"] == "HS사업본부 기계"
    assert (classified["job_role"], classified["experience_type"]) == ("서버·백엔드", "경력")
    assert json.loads(classified["evidence_json"]) == {
        "experience_type_evidence": "5년 이상",
        "job_role_evidence": "서버",
    }
    assert classified["dropped_fields"] == "position_name, responsibilities, job_role"

    assert [
        row["field_name"]
        for row in conn.execute("SELECT field_name FROM job_field_overrides ORDER BY 1")
    ] == [
        "company_name",
        "job_role",
    ]
    assert (
        conn.execute(
            "SELECT value FROM job_field_overrides WHERE field_name = 'job_role'"
        ).fetchone()["value"]
        == "job_minor 보정"
    )
    assert [
        row[0] for row in conn.execute("SELECT field_name FROM job_field_suggestions ORDER BY 1")
    ] == [
        "company_name",
        "job_role",
    ]
    assert [row[0] for row in conn.execute("SELECT field_name FROM normalization_rules")] == [
        "recruitment_end_at"
    ]

    crawler = conn.execute("SELECT selectors_json, api_config_json FROM crawlers").fetchone()
    selectors = json.loads(crawler["selectors_json"])
    assert selectors["list"]["company_name"] == ".company"
    assert selectors["detail"]["recruitment_end_at"] == ".deadline"
    assert selectors["detail"]["qualifications"] == ".req"
    api_config = json.loads(crawler["api_config_json"])
    assert api_config["list"]["fields"] == {"title": "t", "company_name": "companyName"}
    # 요청 본문은 사이트에 보내는 값이라 칸 이름이 아니다
    assert api_config["list"]["body"] == {"company": "사이트 값"}
    assert api_config["detail"]["fields"] == {
        "recruitment_end_at": "data.end",
        "responsibilities": "data.task",
    }

    raw = conn.execute("SELECT raw_data_json FROM raw_jobs ORDER BY id").fetchall()
    assert json.loads(raw[0]["raw_data_json"]) == {
        "title": "백엔드",
        "company_name": "예시",
        "recruitment_end_at": "2026-12-31",
        "body": "본문",
        "recruitment_notice": "안내",
    }
    assert raw[1]["raw_data_json"] == "{끊긴"


def test_old_names_are_rejected_after_up(conn: sqlite3.Connection) -> None:
    db.migrate_up(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO job_field_overrides (raw_job_id, part, field_name, value)"
            " VALUES (1, 2, 'company', '값')"
        )


def test_down_restores_the_old_names(conn: sqlite3.Connection) -> None:
    db.migrate_up(conn)
    _down_to_0030(conn)

    job = conn.execute("SELECT * FROM normalized_jobs").fetchone()
    assert (job["company"], job["deadline"], job["job_minor"]) == (
        "예시",
        "2026-12-31",
        "서버·백엔드",
    )
    # 지운 자유 글자 직무는 되살리지 못한다. 칼럼만 빈 채로 돌아온다
    assert job["job_role"] is None

    classified = conn.execute("SELECT * FROM job_classifications").fetchone()
    assert (classified["job_role"], classified["career_level"]) == ("HS사업본부 기계", "경력")
    assert classified["dropped_fields"] == "job_role, duties, job_minor"

    selectors = json.loads(conn.execute("SELECT selectors_json FROM crawlers").fetchone()[0])
    assert selectors["detail"]["deadline"] == ".deadline"
    raw = json.loads(conn.execute("SELECT raw_data_json FROM raw_jobs ORDER BY id").fetchone()[0])
    assert raw["company"] == "예시" and "company_name" not in raw
