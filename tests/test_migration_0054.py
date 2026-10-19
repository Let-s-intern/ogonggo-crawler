"""0054 가 직무 분류표와 쌓인 공고 값을 오공고 직군 19개·직무 229개로 옮긴다 (LC-3472, 2026-10-19).

실제 `migrations/` 를 0053 까지 적용한 DB 에 옛 씨앗으로 분류표를 넣고, 옛 이름으로 분류된 공고를
넣은 뒤 0054 를 올린다.

| 확인 | 깨지면 |
|---|---|
| 켜진 분류표가 새 씨앗과 이름·순서까지 같다 | AI 가 오공고에 없는 직군·직무를 고른다 |
| 지운 직무는 합친 직군 아래에 꺼진 채 남는다 | 지운 직무 공고가 자동 전송에 섞인다 |
| 공고·보정·제안 값이 새 직군·직무로 옮겨진다 | 공고 목록이 옛 직군을 보이고 분류표와 어긋난다 |
| 운영자가 합쳐진 직군에 넣은 직무도 합친 직군으로 간다 | 직군 행을 지우다 외래키에 막힌다 |
| 빈 표에도 올라간다 | 새로 띄운 서버가 마이그레이션에서 멈춘다 |
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from app import db, taxonomy

SEEDS = pathlib.Path(__file__).parent.parent / "seeds"
OLD_SEED = SEEDS / "job-taxonomy-zighang-20260828.json"
NEW_SEED = SEEDS / "job-taxonomy-ogonggo-20261019.json"

JOBS = [
    ("IT·개발", "서버·백엔드"),
    ("IT·개발", "프론트엔드"),
    ("AI·데이터", "데이터분석가"),
    ("AI·데이터", "RAG"),
    ("디자인", "웹디자인"),
    ("디자인", None),
    ("미디어·엔터", "기타"),
    ("영업", "B2B영업"),
]


def _down_to_0053(connection: sqlite3.Connection) -> None:
    applied = db.applied_versions(connection)
    db.migrate_down(connection, steps=len(applied) - applied.index("0054"))


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "taxonomy.db")
    db.migrate_up(connection)
    _down_to_0053(connection)
    assert db.applied_versions(connection)[-1] == "0053"
    yield connection
    connection.close()


def _seed_old(connection: sqlite3.Connection) -> None:
    taxonomy.load_seed(connection, OLD_SEED)
    design = connection.execute(
        "SELECT id FROM job_taxonomy WHERE parent_id IS NULL AND name = '디자인'"
    ).fetchone()[0]
    taxonomy.create(connection, parent_id=design, name="운영자가 넣은 직무")
    connection.execute("INSERT INTO crawlers (name, list_url) VALUES ('예시', 'https://x')")
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '예시')")
    for index, (field, role) in enumerate(JOBS, start=1):
        url = f"https://x/{index}"
        connection.execute(
            "INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)"
            " VALUES (?, 1, ?, '{}', ?)",
            (index, url, f"h{index}"),
        )
        connection.execute(
            "INSERT INTO normalized_jobs (raw_job_id, source_url, job_field, job_role)"
            " VALUES (?, ?, ?, ?)",
            (index, url, field, role),
        )
        connection.execute(
            "INSERT INTO job_classifications (raw_job_id, model, evidence_json, dropped_fields,"
            " job_field, job_role) VALUES (?, 'm', '{}', '', ?, ?)",
            (index, field, role),
        )
    connection.execute(
        "INSERT INTO job_field_overrides (raw_job_id, field_name, value)"
        " VALUES (1, 'job_field', 'AI·데이터'), (1, 'job_role', '데이터사이언티스트')"
    )
    connection.execute(
        "INSERT INTO job_field_suggestions (raw_job_id, field_name, value)"
        " VALUES (2, 'job_field', '고객상담·TM'), (2, 'job_role', '인바운드')"
    )


def test_분류표와_공고_값을_오공고_직군_직무로_옮긴다(conn: sqlite3.Connection) -> None:
    _seed_old(conn)

    db.migrate_up(conn)

    tree = [
        {"name": major, "minors": [m for m in minors if m != "운영자가 넣은 직무"]}
        for major, minors in taxonomy.enabled_tree(conn)
    ]
    assert tree == json.loads(NEW_SEED.read_text(encoding="utf-8"))["majors"]

    disabled = conn.execute(
        "SELECT p.name, m.name FROM job_taxonomy m JOIN job_taxonomy p ON p.id = m.parent_id"
        " WHERE m.enabled = 0"
    ).fetchall()
    assert len(disabled) == 41
    assert ("기획·전략", "웹디자인") in [tuple(row) for row in disabled]

    assert (
        conn.execute(
            "SELECT p.name FROM job_taxonomy m JOIN job_taxonomy p ON p.id = m.parent_id"
            " WHERE m.name = '운영자가 넣은 직무'"
        ).fetchone()[0]
        == "기획·전략"
    )

    expected = [
        ("IT·개발", "백엔드"),
        ("IT·개발", "프론트 풀스택"),
        ("IT·개발", "데이터분석·사이언티스트"),
        ("IT·개발", "기타IT·개발"),
        ("기획·전략", "웹디자인"),
        ("기획·전략", None),
        ("마케팅·광고", "기타"),
        ("영업", "B2B영업"),
    ]
    for table in ("normalized_jobs", "job_classifications"):
        rows = conn.execute(f"SELECT job_field, job_role FROM {table} ORDER BY raw_job_id")
        assert [tuple(row) for row in rows] == expected, table

    overrides = conn.execute("SELECT value FROM job_field_overrides ORDER BY field_name")
    assert [row[0] for row in overrides] == ["IT·개발", "데이터분석·사이언티스트"]
    suggestions = conn.execute("SELECT value FROM job_field_suggestions ORDER BY field_name")
    assert [row[0] for row in suggestions] == ["HR·총무", "인바운드"]


def test_빈_표에도_올라간다(conn: sqlite3.Connection) -> None:
    db.migrate_up(conn)

    assert db.applied_versions(conn)[-1] == "0054"
    assert taxonomy.is_empty(conn)
