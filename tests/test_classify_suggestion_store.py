"""한 번의 분류 호출이 두 표로 갈라지는지 본다 (11.3.V).

`job_classifications` 는 비어 있던 아홉 칸을 채운 결과이고 `job_field_suggestions` 는 값이
있는 칸(`company`·`deadline`·`start_date`)에 원문이 다른 값을 낸 것이다. 호출은 하나뿐이다
— 두 번 부르면 토큰이 두 배라 `app/classify/batch.py` 의 `classify_ids` 가 같은 응답을
두 저장 함수에 나눠 넣는지를 본다.

Gemini 를 실제로 부르지 않는다.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from app import db
from app.classify.batch import ClassifyProgress, classify_ids
from app.classify.store import read_classification, read_suggestions, save_suggestions
from app.config import Settings
from tests.classify_fakes import response
from tests.test_selector_generator import FakeClient

BODY = (
    "◆ 업무내용\n제휴사 데이터 연동 구조 기획\n\n◆ 지원자격\n관련 경험 5년 이상이신 분\n"
    "◆ 소속 법인\n한화솔루션\n"
)


def settings_with_key() -> Settings:
    return Settings(gemini_api_key="테스트키", gemini_model="gemini-3.5-flash")


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        """
        INSERT INTO crawlers (id, name, list_url, status)
        VALUES (1, '테스트', 'https://x', 'promoted')
        """
    )
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, '테스트')")
    raw = {
        "source_url": "https://x/1",
        "title": "공고 1",
        "body": BODY,
        # 이미 채워져 있는 값. 원문(위 BODY)의 "한화솔루션" 과 다르다
        "company": "한화생명",
    }
    connection.execute(
        """
        INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash)
        VALUES (1, ?, ?, 'hash1')
        """,
        (raw["source_url"], json.dumps(raw, ensure_ascii=False)),
    )
    try:
        yield connection
    finally:
        connection.close()


async def test_one_call_fills_one_table_and_suggests_in_the_other(
    conn: sqlite3.Connection,
) -> None:
    """채운 아홉 칸은 `job_classifications` 로, 값이 다른 회사명은 `job_field_suggestions` 로."""
    answer = response(
        duties="제휴사 데이터 연동 구조 기획",
        company_suggestion="한화솔루션",
        company_suggestion_reason="원문이 말하는 소속 법인이 다르다",
    )

    progress = await classify_ids(
        conn, [1], ClassifyProgress(), client=FakeClient(answer), settings=settings_with_key()
    )

    assert progress.processed == 1
    # 호출은 한 번뿐이다
    assert progress.calls == 1

    classified = read_classification(conn, 1)
    assert classified["duties"] == "제휴사 데이터 연동 구조 기획"

    suggested = read_suggestions(conn, 1)
    assert suggested["company"]["value"] == "한화솔루션"
    assert suggested["company"]["reason"] == "원문이 말하는 소속 법인이 다르다"

    stored = conn.execute(
        "SELECT field_name, value, reason FROM job_field_suggestions WHERE raw_job_id = 1"
    ).fetchall()
    assert [dict(row) for row in stored] == [
        {
            "field_name": "company",
            "value": "한화솔루션",
            "reason": "원문이 말하는 소속 법인이 다르다",
        }
    ]


async def test_no_suggestion_leaves_the_suggestion_table_empty(conn: sqlite3.Connection) -> None:
    """제안할 것이 없으면 아무것도 쓰지 않는다. 빈 값을 저장하지 않는다."""
    answer = response(duties="제휴사 데이터 연동 구조 기획")

    await classify_ids(
        conn, [1], ClassifyProgress(), client=FakeClient(answer), settings=settings_with_key()
    )

    assert read_suggestions(conn, 1) == {}


def test_save_suggestions_overwrites_the_same_column(conn: sqlite3.Connection) -> None:
    """같은 칸에 제안이 둘이면 새 제안이 옛 제안을 덮는다 — 저장 함수 자체의 계약이다."""
    save_suggestions(conn, 1, {"deadline": "2026-09-30"}, {"deadline": "첫 판단"})
    save_suggestions(conn, 1, {"deadline": "2026-10-15"}, {"deadline": "다시 읽은 판단"})

    rows = conn.execute(
        "SELECT value, reason FROM job_field_suggestions WHERE raw_job_id = 1"
    ).fetchall()
    assert [dict(row) for row in rows] == [{"value": "2026-10-15", "reason": "다시 읽은 판단"}]


def test_save_suggestions_leaves_fields_it_was_not_given_alone(conn: sqlite3.Connection) -> None:
    """이번 호출이 company 를 말하지 않았다고 옛 deadline 제안이 사라지면 안 된다."""
    save_suggestions(conn, 1, {"deadline": "2026-09-30"}, {"deadline": "이유"})

    save_suggestions(conn, 1, {"company": "한화솔루션"}, {"company": "다른 이유"})

    rows = {
        row["field_name"]: row["value"]
        for row in conn.execute(
            "SELECT field_name, value FROM job_field_suggestions WHERE raw_job_id = 1"
        ).fetchall()
    }
    assert rows == {"deadline": "2026-09-30", "company": "한화솔루션"}
