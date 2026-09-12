"""분류가 나눈 공고는 번호마다 정규화 행 하나가 된다 (2026-09-11 결정).

나눈 공고는 제목 뒤에 직무 이름이(`원래 제목 - 직무 이름`), 주소 뒤에 번호가(`...#2`) 붙는다.
나누지 않은 공고는 지금과 같다. 실사이트에도 모델에도 나가지 않는다.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from app import db
from app.classify.batch import ClassifyProgress, classify_ids
from app.config import Settings
from app.normalize.backfill import BackfillProgress, renormalize, rewrite_one
from app.normalize.engine import insert_normalized, load_rules
from tests import test_classify_split as split_posting
from tests.test_selector_generator import FakeClient

TITLE = split_posting.TITLE
URL = "https://x/1"
DELIVERED_AT = "2026-09-01 10:00:00"
TWO_ROLES = [(1, "로봇 SW 개발", "로봇 제어"), (2, "비전 AI 연구", "영상 인식")]


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (id, name, list_url, status)"
        " VALUES (1, '테스트', 'https://x', 'promoted')"
    )
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, '테스트')")
    raw = {"source_url": URL, "title": TITLE, "body": split_posting.BODY, "company": "테스트회사"}
    connection.execute(
        "INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)"
        " VALUES (1, 1, ?, ?, 'hash1')",
        (URL, json.dumps(raw, ensure_ascii=False)),
    )
    try:
        yield connection
    finally:
        connection.close()


def classify_as(conn: sqlite3.Connection, parts: list[tuple[int, str | None, str]]) -> None:
    """분류가 끝난 상태. (번호, 직무 이름, 주요 업무)."""
    conn.executemany(
        "INSERT INTO job_classifications (raw_job_id, part, part_role, model, duties)"
        " VALUES (1, ?, ?, 'model', ?)",
        parts,
    )


def normalized(conn: sqlite3.Connection, columns: str) -> list[tuple[object, ...]]:
    rows = conn.execute(f"SELECT {columns} FROM normalized_jobs ORDER BY part").fetchall()
    return [tuple(row) for row in rows]


def test_나눈_공고는_제목에_직무_이름이_주소에_번호가_붙는다(conn: sqlite3.Connection) -> None:
    classify_as(conn, TWO_ROLES)

    rewrite_one(conn, 1, load_rules(conn))

    assert normalized(conn, "part, title, source_url, duties") == [
        (1, f"{TITLE} - 로봇 SW 개발", f"{URL}#1", "로봇 제어"),
        (2, f"{TITLE} - 비전 AI 연구", f"{URL}#2", "영상 인식"),
    ]


def test_나누지_않은_공고는_제목과_주소가_그대로다(conn: sqlite3.Connection) -> None:
    classify_as(conn, [(1, None, "로봇 제어")])

    rewrite_one(conn, 1, load_rules(conn))

    assert normalized(conn, "part, title, source_url, duties") == [(1, TITLE, URL, "로봇 제어")]


def test_나누기_전에_정규화된_행은_1번_공고가_되고_전달_표시가_남는다(
    conn: sqlite3.Connection,
) -> None:
    """수집 직후에는 분류가 없어 1번 행 하나다. 나중에 나뉘어도 그 행을 지우고 새로 넣지 않는다."""
    rules = load_rules(conn)
    first = insert_normalized(conn, 1, rules)
    conn.execute("UPDATE normalized_jobs SET delivered_at = ?", (DELIVERED_AT,))
    classify_as(conn, TWO_ROLES)

    rewrite_one(conn, 1, rules)

    rows = normalized(conn, "id, part, source_url, delivered_at")
    assert rows[0] == (first, 1, f"{URL}#1", DELIVERED_AT)
    assert rows[1][1:] == (2, f"{URL}#2", None)


def test_사람이_고친_값은_그_번호의_공고에만_덮인다(conn: sqlite3.Connection) -> None:
    classify_as(conn, TWO_ROLES)
    conn.execute(
        "INSERT INTO job_field_overrides (raw_job_id, part, field_name, value)"
        " VALUES (1, 2, 'title', '사람이 정한 제목')"
    )

    rewrite_one(conn, 1, load_rules(conn))

    assert normalized(conn, "part, title") == [
        (1, f"{TITLE} - 로봇 SW 개발"),
        (2, "사람이 정한 제목"),
    ]


def test_재정규화도_나눈_공고를_전부_고친다(conn: sqlite3.Connection) -> None:
    classify_as(conn, TWO_ROLES)

    progress = renormalize(conn, BackfillProgress())

    assert progress.processed == 1
    assert normalized(conn, "part, source_url") == [(1, f"{URL}#1"), (2, f"{URL}#2")]


async def test_분류가_나누면_정규화_표에도_나뉘어_들어간다(conn: sqlite3.Connection) -> None:
    progress = ClassifyProgress()

    await classify_ids(
        conn,
        [1],
        progress,
        client=FakeClient(split_posting.SPLIT),
        settings=Settings(gemini_api_key="테스트키", gemini_model="gemini-3.5-flash"),
    )

    assert progress.processed == 1
    assert normalized(conn, "part, title, source_url") == [
        (1, f"{TITLE} - 로봇 SW 개발", f"{URL}#1"),
        (2, f"{TITLE} - 비전 AI 연구", f"{URL}#2"),
    ]


def test_조직_이름이_붙은_직무는_제목과_직무_칸에서_한_줄로_이어진다(
    conn: sqlite3.Connection,
) -> None:
    """LG 처럼 다른 사업부에 같은 직무가 있으면 조직 이름이 함께 와야 제목이 겹치지 않는다."""
    conn.executemany(
        "INSERT INTO job_classifications (raw_job_id, part, part_role, job_role, model)"
        " VALUES (1, ?, ?, ?, 'model')",
        [(1, "HS사업본부\n기계", "HS사업본부\n기계"), (2, "MS사업본부\n기계", "MS사업본부\n기계")],
    )

    rewrite_one(conn, 1, load_rules(conn))

    assert normalized(conn, "part, title, job_role") == [
        (1, f"{TITLE} - HS사업본부 기계", "HS사업본부 기계"),
        (2, f"{TITLE} - MS사업본부 기계", "MS사업본부 기계"),
    ]
