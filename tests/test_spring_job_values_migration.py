"""0033 이 판정 값과 모집 일시를 오공고(Spring) 값으로 옮긴다 (2026-09-14 결정).

실제 `migrations/` 를 0032 까지 적용한 DB 에 옛 값으로 한 벌을 넣고 0033 을 올리고 내린다.

| 확인 | 깨지면 |
|---|---|
| 한글 판정 값이 enum 이름이 되고 목록 밖 값은 그대로다 | 오공고로 못 보낸다 |
| `상시모집` 은 빈 마감일이 되고 날짜만 있던 일시에 시각이 붙는다 | 마감일에 글자가 섞인다 |
| 보정 값도 같이 옮기고 새 칸의 보정·제안을 받는다 | 사람이 고친 한글 값이 정규화에서 되살아난다 |
| 시각을 떼던 규칙을 끄고 시각까지 쓰며 시작일 규칙을 복사한다 | 마감 시각이 사라진다 |
| 규칙 번역이 가져오기(`app/field_values.py`)와 같다 | 옛 파일에서 온 규칙과 이 서버 규칙이 갈린다 |
| 올린 규칙으로 기간과 날짜를 읽는다 | 규칙이 깨져 정규화가 멈춘다 |
| 깨진 JSON 규칙은 건드리지 않는다 | 규칙 한 행 때문에 마이그레이션이 실패한다 |
| 내리면 옛 값과 옛 규칙으로 돌아간다 | 배포를 되돌릴 수 없다 |
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from app import db, field_values
from app.normalize.engine import load_rules, normalize_fields

END = "recruitment_end_at"
START = "recruitment_start_at"
PERIOD_REGEX = json.dumps({"pattern": "^.*?[~〜]\\s*", "replacement": ""})
TIME_STRIP = json.dumps({"pattern": field_values.TIME_STRIP_PATTERN, "replacement": ""})
OLD_DATE_PARSE = json.dumps({"formats": ["%Y-%m-%d", "%Y.%m.%d"], "output_format": "%Y-%m-%d"})
BROKEN = "{깨짐"


def _down_to_0032(connection: sqlite3.Connection) -> None:
    applied = db.applied_versions(connection)
    db.migrate_down(connection, steps=len(applied) - applied.index("0033"))


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    """0032 까지 올린 DB 에 옛 값으로 한 벌을 넣는다."""
    connection = db.connect(tmp_path / "values.db")
    db.migrate_up(connection)
    _down_to_0032(connection)
    assert db.applied_versions(connection)[-1] == "0032"
    connection.execute("INSERT INTO crawlers (name, list_url) VALUES ('예시', 'https://x')")
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '예시')")
    connection.execute(
        """
        INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)
        VALUES (1, 1, 'https://x/1', '{}', 'h1'), (2, 1, 'https://x/2', '{}', 'h2')
        """
    )
    connection.execute(
        """
        INSERT INTO normalized_jobs (raw_job_id, source_url, employment_type, experience_type,
                                     education_level, recruitment_end_at, recruitment_start_at)
        VALUES (1, 'https://x/1', '정규직', '경력', '학사', '2026-09-30', '2026-09-01'),
               (2, 'https://x/2', 'Permanent', '무관', '무관', '상시모집', NULL)
        """
    )
    connection.execute(
        """
        INSERT INTO job_classifications (raw_job_id, model, employment_type, experience_type,
                                         education_level, evidence_json, dropped_fields)
        VALUES (1, 'm', '인턴', '신입', '고졸', '{}', '')
        """
    )
    connection.execute(
        """
        INSERT INTO job_field_overrides (raw_job_id, field_name, value)
        VALUES (1, 'employment_type', '계약직'), (1, 'recruitment_end_at', '2026-10-31'),
               (1, 'recruitment_start_at', '2026-10-01'), (2, 'recruitment_end_at', '상시모집'),
               (2, 'experience_type', '무관'), (2, 'title', '고친 제목')
        """
    )
    connection.execute(
        "INSERT INTO job_field_suggestions (raw_job_id, field_name, value)"
        " VALUES (1, 'company_name', '제안')"
    )
    connection.executemany(
        """
        INSERT INTO normalization_rules (field_name, rule_type, rule_config_json, priority, enabled)
        VALUES (?, ?, ?, ?, 1)
        """,
        [
            (END, "trim", json.dumps({"collapse_whitespace": True}), 0),
            (END, "regex", PERIOD_REGEX, 10),
            (END, "regex", TIME_STRIP, 20),
            (END, "date_parse", OLD_DATE_PARSE, 50),
            (END, "date_parse", BROKEN, 99),
            ("title", "trim", "{}", 0),
        ],
    )
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


def _jobs(conn: sqlite3.Connection) -> list[tuple[object, ...]]:
    return [
        tuple(row)
        for row in conn.execute(
            """
            SELECT employment_type, experience_type, education_level, recruitment_end_at,
                   recruitment_start_at
              FROM normalized_jobs ORDER BY raw_job_id
            """
        )
    ]


def _overrides(conn: sqlite3.Connection) -> dict[tuple[int, str], str]:
    return {
        (int(row["raw_job_id"]), str(row["field_name"])): str(row["value"])
        for row in conn.execute("SELECT raw_job_id, field_name, value FROM job_field_overrides")
    }


def _rules(conn: sqlite3.Connection, field: str) -> list[tuple[str, str, int]]:
    return [
        (str(row["rule_type"]), str(row["rule_config_json"]), int(row["enabled"]))
        for row in conn.execute(
            "SELECT rule_type, rule_config_json, enabled FROM normalization_rules"
            " WHERE field_name = ? ORDER BY priority, id",
            (field,),
        )
    ]


def _comparable_rules(conn: sqlite3.Connection) -> dict[str, list[tuple[str, object, int]]]:
    """칸마다의 규칙. 설정은 JSON 으로 읽어 비교한다 — SQLite 의 json 함수가 공백을 지운다."""

    def config(text: str) -> object:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    return {
        field: [(kind, config(text), enabled) for kind, text, enabled in _rules(conn, field)]
        for field in (END, START, "title")
    }


def test_up_moves_the_judged_values_and_the_dates(conn: sqlite3.Connection) -> None:
    db.migrate_up(conn)

    assert _jobs(conn) == [
        ("FULL_TIME", "EXPERIENCED", "BACHELOR", "2026-09-30 23:59:59", "2026-09-01 00:00:00"),
        # 옛 매핑 값은 무엇을 뜻하는지 정할 수 없어 그대로 둔다
        ("Permanent", "IRRELEVANT", "ANY", None, None),
    ]
    derived = conn.execute(
        "SELECT recruitment_type, auto_close_enabled FROM normalized_jobs ORDER BY raw_job_id"
    ).fetchall()
    assert [tuple(row) for row in derived] == [("PERIOD", "true"), ("ALWAYS_OPEN", "false")]

    classified = conn.execute("SELECT * FROM job_classifications").fetchone()
    assert (
        classified["employment_type"],
        classified["experience_type"],
        classified["education_level"],
    ) == ("INTERN", "NEWCOMER", "HIGH_SCHOOL")
    assert classified["closes_when_filled"] is None


def test_up_moves_the_corrections_and_takes_the_new_fields(conn: sqlite3.Connection) -> None:
    db.migrate_up(conn)

    assert _overrides(conn) == {
        (1, "employment_type"): "CONTRACT",
        (1, "recruitment_end_at"): "2026-10-31 23:59:59",
        (1, "recruitment_start_at"): "2026-10-01 00:00:00",
        # 상시모집 으로 고친 것은 빈 마감일로 고친 것이다
        (2, "recruitment_end_at"): "",
        (2, "experience_type"): "IRRELEVANT",
        (2, "title"): "고친 제목",
    }
    conn.execute(
        "INSERT INTO job_field_overrides (raw_job_id, field_name, value)"
        " VALUES (1, 'application_method', 'EMAIL')"
    )
    conn.execute(
        "INSERT INTO job_field_suggestions (raw_job_id, field_name, value)"
        " VALUES (1, 'closes_when_filled', 'true')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO job_field_overrides (raw_job_id, field_name, value)"
            " VALUES (1, 'recruitment_type', 'PERIOD')"
        )


def test_up_turns_the_date_rules_to_datetimes_and_copies_them_to_the_start(
    conn: sqlite3.Connection,
) -> None:
    db.migrate_up(conn)

    end = _rules(conn, END)
    assert end[2] == ("regex", TIME_STRIP, 0)
    migrated = json.loads(end[3][1])
    assert migrated["output_format"] == "%Y-%m-%d %H:%M:%S"
    assert migrated["formats"] == ["%Y-%m-%d", "%Y.%m.%d", *field_values.DATETIME_FORMATS]
    # 가져오기가 옛 파일의 규칙을 옮기는 모양과 같다
    translated, enabled = field_values.rule(END, "date_parse", OLD_DATE_PARSE, 1)
    assert (json.loads(translated), enabled) == (migrated, 1)
    assert field_values.rule(END, "regex", TIME_STRIP, 1) == (TIME_STRIP, 0)
    assert end[4] == ("date_parse", BROKEN, 1)

    assert _rules(conn, START) == end
    assert _rules(conn, "title") == [("trim", "{}", 1)]


def test_the_migrated_rules_read_periods_and_bare_dates(conn: sqlite3.Connection) -> None:
    db.migrate_up(conn)
    conn.execute("DELETE FROM normalization_rules WHERE rule_config_json = ?", (BROKEN,))

    rules = load_rules(conn)
    period = normalize_fields({END: "2026-08-15 09:00 ~ 2026-08-30 17:00"}, rules)
    bare = normalize_fields({END: "2026.08.31"}, rules)

    assert (period[START], period[END]) == ("2026-08-15 09:00:00", "2026-08-30 17:00:00")
    assert (bare[START], bare[END]) == (None, "2026-08-31 23:59:59")


def test_existing_start_rules_are_not_overwritten_by_a_copy(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO normalization_rules (field_name, rule_type, rule_config_json)"
        " VALUES (?, 'trim', '{}')",
        (START,),
    )

    db.migrate_up(conn)

    assert _rules(conn, START) == [("trim", "{}", 1)]


def test_down_restores_the_old_values_and_rules(conn: sqlite3.Connection) -> None:
    before_rules = _comparable_rules(conn)
    db.migrate_up(conn)
    conn.execute(
        "INSERT INTO job_field_overrides (raw_job_id, field_name, value)"
        " VALUES (1, 'closes_when_filled', 'true')"
    )
    conn.execute(
        "INSERT INTO job_field_suggestions (raw_job_id, field_name, value)"
        " VALUES (1, 'application_method', 'EMAIL')"
    )

    db.migrate_down(conn, steps=1)

    assert _jobs(conn) == [
        ("정규직", "경력", "학사", "2026-09-30", "2026-09-01"),
        ("Permanent", "무관", "무관", "상시모집", None),
    ]
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(normalized_jobs)")}
    assert "recruitment_type" not in columns
    assert _overrides(conn) == {
        (1, "employment_type"): "계약직",
        (1, "recruitment_end_at"): "2026-10-31",
        (1, "recruitment_start_at"): "2026-10-01",
        # 빈 마감일로 고친 것은 되돌려도 빈 값이다. 옛 정규화가 그 자리를 상시모집 으로 채운다
        (2, "recruitment_end_at"): "",
        (2, "experience_type"): "무관",
        (2, "title"): "고친 제목",
    }
    assert [row[0] for row in conn.execute("SELECT field_name FROM job_field_suggestions")] == [
        "company_name"
    ]
    assert _comparable_rules(conn) == before_rules


def test_down_folds_the_values_the_old_list_did_not_have(conn: sqlite3.Connection) -> None:
    db.migrate_up(conn)
    conn.execute(
        "UPDATE normalized_jobs SET employment_type = 'PART_TIME', experience_type = 'BOTH'"
        " WHERE raw_job_id = 1"
    )

    db.migrate_down(conn, steps=1)

    row = conn.execute(
        "SELECT employment_type, experience_type FROM normalized_jobs WHERE raw_job_id = 1"
    ).fetchone()
    assert tuple(row) == ("기타", "무관")
