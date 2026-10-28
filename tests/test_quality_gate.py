"""품질 검사가 다시 분류할 공고를 고르는 네 조건 (2026-10-21 결정, `app/classify/gate.py`)."""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from app import db
from app.classify import gate
from app.classify.gate import CAREER_CAP, EMPTY_KEY, OFF_TARGET, THIN_CONTENT

NOW = "2026-10-21 09:00:00"

# 네 조건 어디에도 걸리지 않는 공고
COMPLETE: dict[str, Any] = {
    "job_field": "IT·개발",
    "job_role": "백엔드",
    "recruitment_end_at": "2026-11-30 23:59:00",
    "recruitment_type": "PERIOD",
    "employment_type": "FULL_TIME",
    "experience_type": "NEWCOMER",
    "experience_min_years": "0",
    "experience_max_years": None,
    **{name: "내용" for name in gate.CONTENT_FIELDS},
}


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (id, name, list_url, status)"
        " VALUES (1, '테스트', 'https://x', 'promoted')"
    )
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, '테스트')")
    try:
        yield connection
    finally:
        connection.close()


def add(conn: sqlite3.Connection, raw_id: int, part: int = 1, **values: Any) -> int:
    """정규화까지 끝난 공고 한 건. `COMPLETE` 에서 준 칸만 바꾼다."""
    url = f"https://x/{raw_id}"
    conn.execute(
        "INSERT OR IGNORE INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)"
        " VALUES (?, 1, ?, '{}', ?)",
        (raw_id, url, f"hash{raw_id}"),
    )
    row = {**COMPLETE, **values}
    names = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO normalized_jobs (raw_job_id, part, source_url, {names})"
        f" VALUES (?, ?, ?, {marks})",
        (raw_id, part, url if part == 1 else f"{url}#{part}", *row.values()),
    )
    return raw_id


def reasons(conn: sqlite3.Connection, raw_id: int) -> list[str]:
    return gate.reasons_of(conn, [raw_id], NOW).get(raw_id, [])


def test_a_complete_posting_passes(conn: sqlite3.Connection) -> None:
    assert reasons(conn, add(conn, 1)) == []


@pytest.mark.parametrize("name", gate.KEY_FIELDS)
def test_any_of_the_four_key_fields_empty_is_caught(conn: sqlite3.Connection, name: str) -> None:
    assert reasons(conn, add(conn, 1, **{name: None})) == [EMPTY_KEY]


def test_an_always_open_posting_without_a_deadline_is_caught_too(
    conn: sqlite3.Connection,
) -> None:
    """AI 가 상시 채용으로 잘못 읽었을 수 있다 (2026-10-21 사용자 결정)."""
    raw_id = add(conn, 1, recruitment_end_at=None, recruitment_type="ALWAYS_OPEN")

    assert reasons(conn, raw_id) == [EMPTY_KEY]


def test_four_empty_content_fields_are_caught_but_three_are_not(
    conn: sqlite3.Connection,
) -> None:
    four = add(conn, 1, **{name: "" for name in gate.CONTENT_FIELDS[:4]})
    three = add(conn, 2, **{name: None for name in gate.CONTENT_FIELDS[:3]})

    assert reasons(conn, four) == [THIN_CONTENT]
    assert reasons(conn, three) == []


def test_a_field_outside_the_target_fields_is_caught(conn: sqlite3.Connection) -> None:
    assert reasons(conn, add(conn, 1, job_field="디자인")) == [OFF_TARGET]
    assert reasons(conn, add(conn, 2, job_field=None)) == [OFF_TARGET]


def test_the_target_fields_follow_the_setting(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES ('collect_target_fields', '[\"영업\"]')"
    )

    assert reasons(conn, add(conn, 1)) == [OFF_TARGET]


@pytest.mark.parametrize(
    ("experience_type", "min_years", "max_years", "caught"),
    [
        # 신입이 지원할 수 없는 최소 연수가 있는데 상한을 모른다
        ("EXPERIENCED", "1", None, True),
        ("EXPERIENCED", "3", None, True),
        # 상한을 안다
        ("EXPERIENCED", "1", "3", False),
        # 최소 연수가 없는 경력 공고는 통과다
        ("EXPERIENCED", None, None, False),
        # 신입이 지원할 수 있으면 최소 경력은 0년이다 (2026-10-22 결정)
        ("NEWCOMER", "0", None, False),
        ("BOTH", None, None, False),
        ("IRRELEVANT", None, None, False),
    ],
)
def test_an_experienced_posting_with_a_minimum_but_no_cap_is_caught(
    conn: sqlite3.Connection,
    experience_type: str,
    min_years: str | None,
    max_years: str | None,
    caught: bool,
) -> None:
    raw_id = add(
        conn,
        1,
        experience_type=experience_type,
        experience_min_years=min_years,
        experience_max_years=max_years,
    )

    assert reasons(conn, raw_id) == ([CAREER_CAP] if caught else [])


def test_one_part_caught_puts_the_whole_posting_in(conn: sqlite3.Connection) -> None:
    """다시 분류는 공고 한 건 단위다. 나눈 공고는 번호 하나만 걸려도 다시 돈다."""
    add(conn, 1, part=1)
    add(conn, 1, part=2, job_role=None, job_field="디자인")

    assert reasons(conn, 1) == [EMPTY_KEY, OFF_TARGET]


def test_sent_and_closed_postings_are_left_alone(conn: sqlite3.Connection) -> None:
    sent = add(conn, 1, job_role=None)
    conn.execute(
        "INSERT INTO spring_deliveries (source_url, status, spring_job_id)"
        " VALUES ('https://x/1', 'sent', 10)"
    )
    closed = add(conn, 2, job_role=None, recruitment_end_at="2026-10-01 23:59:00")

    assert gate.reasons_of(conn, [sent, closed], NOW) == {}


def test_a_posting_is_rechecked_twice_at_most(conn: sqlite3.Connection) -> None:
    raw_id = add(conn, 1, job_role=None)
    conn.execute(
        "INSERT INTO job_classifications (raw_job_id, part, model) VALUES (?, 1, 'm')", (raw_id,)
    )
    picked = gate.pick(conn, [raw_id], NOW)
    assert picked == {raw_id: [EMPTY_KEY]}

    gate.record(conn, picked)
    assert gate.pick(conn, [raw_id], NOW) == picked
    gate.record(conn, picked)

    assert gate.pick(conn, [raw_id], NOW) == {}
    assert gate.backlog(conn, NOW) == {}
    attempts = conn.execute("SELECT attempts FROM quality_gate_runs").fetchone()[0]
    assert attempts == gate.MAX_RECHECKS


def test_blocked_postings_are_the_three_that_go_to_a_person(conn: sqlite3.Connection) -> None:
    """대상 직군 밖은 확인 필요가 아니라 전송 제외다 — 전송의 직군 조건이 막는다."""
    ids = {
        "empty_key": add(conn, 1, job_role=None),
        "thin_content": add(conn, 2, **{name: "" for name in gate.CONTENT_FIELDS[:4]}),
        "career_cap": add(conn, 3, experience_type="EXPERIENCED", experience_min_years="1"),
        "off_target": add(conn, 4, job_field="디자인"),
        "complete": add(conn, 5),
    }
    rows = conn.execute(
        f"SELECT raw_job_id FROM normalized_jobs n WHERE {gate.blocked_sql()} ORDER BY raw_job_id"
    ).fetchall()

    assert [row[0] for row in rows] == [ids["empty_key"], ids["thin_content"], ids["career_cap"]]


def test_finish_keeps_what_is_still_caught(conn: sqlite3.Connection) -> None:
    fixed = add(conn, 1, job_role=None)
    stuck = add(conn, 2, recruitment_end_at=None)
    gate.record(conn, gate.pick(conn, [fixed, stuck], NOW))
    conn.execute("UPDATE normalized_jobs SET job_role = '백엔드' WHERE raw_job_id = ?", (fixed,))

    gate.finish(conn, [fixed, stuck], NOW)

    rows = conn.execute(
        "SELECT raw_job_id, reasons, reasons_after, finished_at IS NOT NULL AS finished"
        " FROM quality_gate_runs ORDER BY raw_job_id"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (fixed, EMPTY_KEY, "", 1),
        (stuck, EMPTY_KEY, EMPTY_KEY, 1),
    ]


def test_the_backlog_skips_unclassified_postings(conn: sqlite3.Connection) -> None:
    """분류가 아직 안 된 공고는 분류 끝에서 검사가 본다."""
    classified = add(conn, 1, job_role=None)
    add(conn, 2, job_role=None)
    conn.execute(
        "INSERT INTO job_classifications (raw_job_id, part, model) VALUES (?, 1, 'm')",
        (classified,),
    )

    assert gate.backlog(conn, NOW) == {classified: [EMPTY_KEY]}
