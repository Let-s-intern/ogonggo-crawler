"""규칙 위에 사람 보정을 덮는 순서 테스트.

이 Push 의 전제가 여기서 확인된다. 사람이 검수한 값을 `normalized_jobs` 에 그냥 쓰면 다음
재정규화가 규칙으로 덮어써서 사라진다. 보정을 `job_field_overrides` 에 따로 쌓고 규칙 다음에
적용하면 둘 다 산다.

확인하는 것은 다섯이다.

- 보정한 필드는 재정규화 후에도 사람 값을 유지한다
- 보정하지 않은 필드는 새 규칙을 반영한다
- 보정을 지우면 다음 정규화에서 규칙이 만든 값으로 돌아간다
- 정규화에 실패했던 건이 뒤늦게 들어올 때도 보정이 붙는다
- `raw_jobs` 는 바이트 단위로 그대로고 `delivered_at` 도 그대로다

픽스처로 돈다. 실사이트에 나가지 않는다.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from app import db
from app.crawler.runner import run_workflow
from app.normalize.backfill import BackfillProgress, renormalize
from app.normalize.engine import OVERRIDABLE_FIELDS
from tests.test_normalize_engine import raw_snapshot
from tests.test_normalize_pipeline import (
    LIST_URL,
    SELECTORS,
    add_rule,
    stub_fetcher,
)

DELIVERED_AT = "2026-08-20T09:00:00+00:00"
DEFAULT_COMPANY = "운영자가 적은 회사"

HUMAN_TITLE = "사람이 정한 제목"
RULE_TITLE = "규칙이 만든 제목"
RULE_BODY = "규칙이 만든 본문"

# 어느 원문 값과도 같지 않은 키. `mapping` 규칙이 항상 `default` 로 떨어지게 만든다
UNMATCHED = "어느 공고에도 없는 값"


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        """
        INSERT INTO crawlers (name, list_url, selectors_json, status, default_company)
        VALUES (?, ?, ?, 'promoted', ?)
        """,
        ("python.org", LIST_URL, json.dumps(SELECTORS), DEFAULT_COMPANY),
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, 'python.org')")
    try:
        yield connection
    finally:
        connection.close()


async def collect(conn: sqlite3.Connection) -> None:
    """1회 수집하고 전달 표시를 붙인다. 검수 대상은 이미 정규화된 데이터다."""
    await run_workflow(conn, 1, fetcher=stub_fetcher(), limit=2)
    conn.execute("UPDATE normalized_jobs SET delivered_at = ?", (DELIVERED_AT,))


def set_override(conn: sqlite3.Connection, raw_job_id: int, field_name: str, value: str) -> None:
    conn.execute(
        "INSERT INTO job_field_overrides (raw_job_id, field_name, value) VALUES (?, ?, ?)",
        (raw_job_id, field_name, value),
    )


def add_late_rules(conn: sqlite3.Connection) -> None:
    """검수 뒤에 운영자가 손본 규칙. 두 필드를 모두 고정된 값으로 만든다.

    `map` 은 비워 둘 수 없으므로 어디에도 맞지 않는 키를 하나 넣고 `default` 로 값을 정한다.
    """
    add_rule(conn, "title", "mapping", {"map": {UNMATCHED: "쓰이지 않는다"}, "default": RULE_TITLE})
    add_rule(
        conn,
        "body",
        "mapping",
        {"map": {UNMATCHED: "쓰이지 않는다"}, "default": RULE_BODY},
    )


def run_renormalize(conn: sqlite3.Connection) -> BackfillProgress:
    progress = renormalize(conn, BackfillProgress())
    assert progress.failed == 0, progress.errors
    return progress


def normalized(conn: sqlite3.Connection, raw_job_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM normalized_jobs WHERE raw_job_id = ?", (raw_job_id,)
    ).fetchone()
    assert row is not None
    return row


async def test_override_survives_a_later_rule_change(conn: sqlite3.Connection) -> None:
    """보정한 필드는 사람 값을 유지하고, 보정 안 한 필드는 새 규칙을 받는다."""
    await collect(conn)
    set_override(conn, 1, "title", HUMAN_TITLE)
    add_late_rules(conn)

    run_renormalize(conn)

    row = normalized(conn, 1)
    assert row["title"] == HUMAN_TITLE
    assert row["body"] == RULE_BODY

    other = normalized(conn, 2)
    assert other["title"] == RULE_TITLE, "보정이 없는 건은 규칙 값 그대로다"


async def test_override_survives_being_renormalized_twice(conn: sqlite3.Connection) -> None:
    """한 번 살아남는 것으로는 부족하다. 몇 번을 돌려도 같은 값이어야 한다."""
    await collect(conn)
    set_override(conn, 1, "title", HUMAN_TITLE)
    add_late_rules(conn)

    run_renormalize(conn)
    run_renormalize(conn)

    assert normalized(conn, 1)["title"] == HUMAN_TITLE


async def test_removing_the_override_returns_the_rule_value(conn: sqlite3.Connection) -> None:
    await collect(conn)
    set_override(conn, 1, "title", HUMAN_TITLE)
    add_late_rules(conn)
    run_renormalize(conn)

    conn.execute("DELETE FROM job_field_overrides WHERE raw_job_id = 1 AND field_name = 'title'")
    run_renormalize(conn)

    assert normalized(conn, 1)["title"] == RULE_TITLE


async def test_override_is_applied_when_the_row_is_first_inserted(
    conn: sqlite3.Connection,
) -> None:
    """정규화에 실패했던 건이 뒤늦게 들어오는 경로에도 보정이 붙는다."""
    add_rule(conn, "title", "date_parse", {"formats": ["%Y.%m.%d"]})
    await run_workflow(conn, 1, fetcher=stub_fetcher(), limit=1)
    assert conn.execute("SELECT count(*) AS n FROM normalized_jobs").fetchone()["n"] == 0

    set_override(conn, 1, "title", HUMAN_TITLE)
    conn.execute("DELETE FROM normalization_rules")
    run_renormalize(conn)

    assert normalized(conn, 1)["title"] == HUMAN_TITLE


async def test_company_override_does_not_touch_the_parent_column(
    conn: sqlite3.Connection,
) -> None:
    """보정은 자회사 칸에만 걸린다. 모회사는 크롤러가 정하는 값이라 보정 대상이 아니다."""
    await collect(conn)
    before = normalized(conn, 1)
    # 이 사이트는 회사명을 주지 않는다. 자회사가 비고 모회사만 남는 것이 맞는 모양이다
    assert (before["parent_company_name"], before["company_name"]) == (DEFAULT_COMPANY, None)

    set_override(conn, 1, "company_name", "사람이 정한 회사")
    run_renormalize(conn)

    row = normalized(conn, 1)
    assert row["company_name"] == "사람이 정한 회사"
    assert row["parent_company_name"] == DEFAULT_COMPANY


async def test_empty_override_clears_the_field(conn: sqlite3.Connection) -> None:
    """빈 문자열은 "비어 있는 것이 맞다" 는 판단이다. 값 없음은 NULL 하나로만 나타난다."""
    await collect(conn)
    set_override(conn, 1, "company_name", "")
    set_override(conn, 1, "region", "")

    run_renormalize(conn)

    row = normalized(conn, 1)
    assert row["company_name"] is None
    assert row["parent_company_name"] == DEFAULT_COMPANY, (
        "자회사를 비운 것이 모회사를 지우지 않는다"
    )
    assert row["region"] is None


async def test_overrides_leave_raw_and_delivery_untouched(conn: sqlite3.Connection) -> None:
    """보정과 재정규화는 수집 데이터도 전달 표시도 건드리지 않는다."""
    await collect(conn)
    before_raw = raw_snapshot(conn)
    before_hashes = [
        row["content_hash"] for row in conn.execute("SELECT content_hash FROM raw_jobs ORDER BY id")
    ]

    set_override(conn, 1, "title", HUMAN_TITLE)
    set_override(conn, 2, "body", "사람이 정리한 본문")
    add_late_rules(conn)
    run_renormalize(conn)
    conn.execute("DELETE FROM job_field_overrides WHERE raw_job_id = 2")
    run_renormalize(conn)

    assert raw_snapshot(conn) == before_raw
    assert [
        row["content_hash"] for row in conn.execute("SELECT content_hash FROM raw_jobs ORDER BY id")
    ] == before_hashes
    delivered = [
        row["delivered_at"]
        for row in conn.execute("SELECT delivered_at FROM normalized_jobs ORDER BY id")
    ]
    assert delivered == [DELIVERED_AT, DELIVERED_AT]


async def test_a_correction_on_a_new_column_survives_renormalization(
    conn: sqlite3.Connection,
) -> None:
    """0012 가 넓힌 자리다. 새 칸도 사람이 고칠 수 있고 그 값이 살아남아야 한다.

    보정은 규칙 다음에 덧씌워진다. 새 칸이 CHECK 에 막혀 있으면 저장 자체가 안 되고, 목록
    (`OVERRIDABLE_FIELDS`)이 좁으면 저장은 되지만 정규화가 읽고 버린다.
    """
    await collect(conn)

    set_override(conn, 1, "region", "사람이 정한 근무지")
    run_renormalize(conn)
    run_renormalize(conn)

    assert normalized(conn, 1)["region"] == "사람이 정한 근무지"


async def test_every_normalized_column_can_be_corrected(conn: sqlite3.Connection) -> None:
    """열여섯 칸 전부다. 자동으로 뽑은 값이 틀렸을 때 고칠 길이 없는 칸을 남기지 않는다."""
    await collect(conn)

    values = {field: f"{field} 를 사람이 고쳤다" for field in OVERRIDABLE_FIELDS}
    # 정규화가 오공고 모양으로 마무리하는 칸은 그 모양으로 고친다. 모집 인원은 숫자로 읽히고,
    # 최소 경력 연수는 경력 공고에만 남는다 (`app/normalize/engine.py` 의 `settle_fields`)
    values.update(
        {
            "recruitment_headcount": "3명",
            "experience_type": "EXPERIENCED",
            "experience_min_years": "5",
        }
    )
    for field, value in values.items():
        set_override(conn, 1, field, value)
    run_renormalize(conn)

    row = normalized(conn, 1)
    expected = {**values, "recruitment_headcount": "3"}
    assert {field: row[field] for field in OVERRIDABLE_FIELDS} == expected


async def test_an_override_on_a_dropped_field_does_not_break_renormalization(
    conn: sqlite3.Connection,
) -> None:
    """0016 이 지운 칸의 보정 행은 남겨 두고 읽지 않는다.

    지우지 않는 것은 되돌릴 때 필요해서다. 읽히지 않는 것은 `apply_overrides` 가
    `OVERRIDABLE_FIELDS` 밖의 필드를 건너뛰기 때문이고, 그래서 그 행이 남아 있어도
    재정규화가 실패하지 않는다 (`migrations/0016_drop_department_category_headcount.sql`).
    """
    await collect(conn)
    set_override(conn, 1, "department", "사람이 고친 부서")
    set_override(conn, 1, "title", HUMAN_TITLE)

    run_renormalize(conn)

    assert normalized(conn, 1)["title"] == HUMAN_TITLE
    # 행은 그대로 남아 있다. 되돌릴 때 이것이 있어야 검수 결과가 살아난다
    kept = conn.execute(
        "SELECT value FROM job_field_overrides WHERE raw_job_id = 1 AND field_name = 'department'"
    ).fetchone()
    assert kept["value"] == "사람이 고친 부서"
