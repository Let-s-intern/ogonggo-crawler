"""정규화가 회사 행을 만드는지 본다.

확인하는 것은 여덟이다.

- 같은 회사 공고가 여러 건이어도 행은 하나다
- 자회사가 빈 건은 모회사 이름으로 행이 생긴다
- 자회사가 있으면 자회사 이름으로 생기고 `parent_name` 에 모회사가 앉는다
- 자회사가 있어도 모회사가 자기 행을 따로 갖는다 (2026-08-29 결정). 안 그러면 회사
  화면에서 모회사 로고를 걸 자리가 없다
- 행에는 로고가 없다. 채우는 것은 운영자다
- 값을 미리 보는 경로는 행을 만들지 않는다
- 스냅샷을 들여오는 경로도 같은 자리를 지나 행을 만든다
- 재정규화도 행을 만든다. 이미 쌓인 공고에는 그것이 유일한 등록 길이다

픽스처로 돈다. 실사이트에 나가지 않는다.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from app import companies, db
from app.api.import_data import import_database
from app.crawler.runner import run_workflow
from app.normalize.backfill import BackfillProgress, renormalize
from app.normalize.engine import insert_normalized, normalized_values
from app.normalize.rules import build_rule
from tests.test_company_selector import (
    WITH_COMPANY,
    WITHOUT_COMPANY,
    make_conn,
    stub_fetcher,
)

# 저장소의 실제 스냅샷. 이 표가 생기기 전(0007)에 뜬 파일이라 `companies` 가 없다
SNAPSHOT = pathlib.Path(__file__).resolve().parent.parent / "seeds" / "snapshot" / "jobs.db"


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        """
        INSERT INTO crawlers (id, name, list_url, status, default_company)
        VALUES (1, '삼성', 'https://x', 'promoted', '삼성전자')
        """
    )
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, '삼성')")
    try:
        yield connection
    finally:
        connection.close()


def add_raw(conn: sqlite3.Connection, company_name: str, seq: int) -> int:
    record = {"title": f"공고 {seq}", "body": "본문", "company_name": company_name}
    cursor = conn.execute(
        """
        INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash)
        VALUES (1, ?, ?, ?)
        """,
        (f"https://x/{seq}", json.dumps(record, ensure_ascii=False), f"hash-{seq}"),
    )
    return int(cursor.lastrowid or 0)


def names(conn: sqlite3.Connection) -> list[tuple[str, str | None, str | None]]:
    return [
        (company.name, company.parent_name, company.logo_url)
        for company in companies.list_all(conn)
    ]


def test_a_company_seen_for_the_first_time_gets_a_row(conn: sqlite3.Connection) -> None:
    insert_normalized(conn, add_raw(conn, "삼성SDS", 1), [])

    assert ("삼성SDS", "삼성전자", None) in names(conn)


def test_the_parent_gets_its_own_row_too(conn: sqlite3.Connection) -> None:
    """모회사 로고를 걸려면 모회사도 자기 이름의 행을 가져야 한다."""
    insert_normalized(conn, add_raw(conn, "삼성SDS", 1), [])

    assert ("삼성전자", None, None) in names(conn)
    assert len(names(conn)) == 2


def test_many_postings_of_one_company_stay_one_row(conn: sqlite3.Connection) -> None:
    """4.3.V 의 앞쪽이다. 공고마다 행이 생기면 화면이 같은 회사를 100번 묻는다."""
    for seq in (1, 2, 3):
        insert_normalized(conn, add_raw(conn, "삼성SDS", seq), [])

    assert names(conn) == [("삼성SDS", "삼성전자", None), ("삼성전자", None, None)]


def test_a_posting_without_a_subsidiary_registers_the_parent(conn: sqlite3.Connection) -> None:
    """4.3.V 의 뒤쪽이다. 자회사를 말하지 않는 사이트는 모회사가 곧 그 회사다."""
    insert_normalized(conn, add_raw(conn, "", 1), [])

    assert names(conn) == [("삼성전자", None, None)]


def test_the_name_is_what_the_rules_produced(conn: sqlite3.Connection) -> None:
    """`삼성전기(주)` 와 `삼성전기` 가 두 행이 되면 로고가 절반의 공고에만 붙는다.

    이름을 맞추는 것은 `company_name` 에 걸린 mapping 규칙의 일이고, 회사 행은 규칙을 지난 값을
    받는다 (`seeds/normalization-rules.json`).
    """
    rule = build_rule("company_name", "mapping", {"map": {"삼성전기(주)": "삼성전기"}})

    insert_normalized(conn, add_raw(conn, "삼성전기(주)", 1), [rule])
    insert_normalized(conn, add_raw(conn, "삼성전기", 2), [rule])

    assert ("삼성전기", "삼성전자", None) in names(conn)
    assert ("삼성전자", None, None) in names(conn)


def test_previewing_a_value_does_not_register_anything(conn: sqlite3.Connection) -> None:
    """규칙 화면의 미리보기가 회사를 늘리면 목록에 공고 없는 회사가 쌓인다."""
    raw_job_id = add_raw(conn, "삼성SDS", 1)

    normalized_values(conn, raw_job_id, [])

    assert companies.list_all(conn) == []


def test_an_operator_edit_survives_the_next_posting(conn: sqlite3.Connection) -> None:
    """행을 덮으면 운영자가 올린 로고가 다음 수집에 사라진다."""
    insert_normalized(conn, add_raw(conn, "삼성SDS", 1), [])
    companies.set_logo_url(conn, "삼성SDS", "https://cdn.test/sds.png")
    companies.set_parent_name(conn, "삼성SDS", "삼성")

    insert_normalized(conn, add_raw(conn, "삼성SDS", 2), [])

    assert ("삼성SDS", "삼성", "https://cdn.test/sds.png") in names(conn)


async def test_a_run_registers_each_affiliate_once(tmp_path: pathlib.Path) -> None:
    """계열사가 섞인 목록. 로고는 공고에 나오는 이름에 붙으므로 자회사가 행이 된다."""
    conn = make_conn(tmp_path / "jobs.db", WITH_COMPANY, default_company="삼성전자")
    try:
        await run_workflow(conn, 1, fetcher=stub_fetcher(), limit=2)

        assert names(conn) == [
            ("삼성SDS", "삼성전자", None),
            ("삼성전기(주)", "삼성전자", None),
            ("삼성전자", None, None),
        ]
    finally:
        conn.close()


async def test_a_run_without_company_selectors_registers_the_parent_once(
    tmp_path: pathlib.Path,
) -> None:
    """두 건 모두 자회사가 없다. 모회사 이름으로 행 하나다."""
    conn = make_conn(tmp_path / "jobs.db", WITHOUT_COMPANY, default_company="삼성전자")
    try:
        await run_workflow(conn, 1, fetcher=stub_fetcher(), limit=2)

        assert names(conn) == [("삼성전자", None, None)]
    finally:
        conn.close()


def test_nothing_is_registered_when_both_columns_are_empty(conn: sqlite3.Connection) -> None:
    """이름 없는 회사 행은 어느 공고와도 이어지지 않는다."""
    assert companies.register(conn, None, None) is None
    assert companies.register(conn, "  ", "") is None

    assert companies.list_all(conn) == []


def test_importing_a_snapshot_registers_its_companies(tmp_path: pathlib.Path) -> None:
    """올린 파일에 `companies` 가 없어도 거절되지 않고, 이 서버가 행을 만든다.

    가져오기는 저쪽의 `normalized_jobs` 를 읽지 않고 이 서버의 규칙으로 다시 정규화한다
    (`app/api/import_data.py`). 그 경로도 `insert_normalized` 를 지나므로 회사 행은 여기서
    생긴다 — 들여온 공고만 화면에서 회사가 없는 상태로 남지 않는다.
    """
    conn = db.connect(tmp_path / "server.db")
    db.migrate_up(conn)
    try:
        result = import_database(conn, SNAPSHOT)

        assert result.normalized_added > 0
        registered = names(conn)
        assert registered, "들여온 공고에 회사 행이 하나도 생기지 않았다"
        assert all(logo_url is None for _, _, logo_url in registered)
        # 이름은 정규화가 확정한 두 칸에서 온다. 공고 수가 아니라 회사 수만큼 생긴다
        stored = {
            str(row["name"])
            for row in conn.execute(
                "SELECT DISTINCT coalesce(company_name, parent_company_name) AS name"
                " FROM normalized_jobs"
                " WHERE coalesce(company_name, parent_company_name) IS NOT NULL"
            )
        }
        # 자회사가 있는 행도 모회사가 자기 행을 따로 갖는다 (2026-08-29 결정). 그래서
        # 등록된 이름 집합은 위 `coalesce` 집합에 실제 모회사들을 더한 것과 같다
        parents = {
            str(row["parent_company_name"])
            for row in conn.execute(
                "SELECT DISTINCT parent_company_name FROM normalized_jobs"
                " WHERE parent_company_name IS NOT NULL"
            )
        }
        assert {name for name, _, _ in registered} == stored | parents
    finally:
        conn.close()


def add_rule(conn: sqlite3.Connection, config: dict[str, object]) -> None:
    """`company_name` 에 mapping 규칙 하나. 재정규화가 DB 에서 규칙을 읽으므로 표에 넣는다."""
    conn.execute(
        """
        INSERT INTO normalization_rules (field_name, rule_type, rule_config_json, priority)
        VALUES ('company_name', 'mapping', ?, 0)
        """,
        (json.dumps(config, ensure_ascii=False),),
    )


def test_renormalizing_registers_a_company_that_had_no_row(conn: sqlite3.Connection) -> None:
    """4.5.V 의 앞쪽. 이 표가 생기기 전에 정규화된 공고는 재정규화로만 회사를 얻는다."""
    raw_job_id = add_raw(conn, "삼성SDS", 1)
    conn.execute(
        """
        INSERT INTO normalized_jobs (raw_job_id, source_url, company_name, parent_company_name)
        VALUES (?, 'https://x/1', '삼성SDS', '삼성전자')
        """,
        (raw_job_id,),
    )
    assert companies.list_all(conn) == []

    renormalize(conn, BackfillProgress())

    assert ("삼성SDS", "삼성전자", None) in names(conn)


def test_renormalizing_after_a_rename_rule_registers_the_new_name(
    conn: sqlite3.Connection,
) -> None:
    """4.5.V 의 뒤쪽. 규칙으로 이름을 고쳤는데 행이 없으면 로고를 붙일 회사를 찾지 못한다.

    옛 이름의 행은 남는다. 지우는 것은 운영자가 한다.
    """
    insert_normalized(conn, add_raw(conn, "삼성전기(주)", 1), [])
    add_rule(conn, {"map": {"삼성전기(주)": "삼성전기"}})

    renormalize(conn, BackfillProgress())

    assert ("삼성전기", "삼성전자", None) in names(conn)
    assert ("삼성전기(주)", "삼성전자", None) in names(conn)


def test_renormalizing_twice_keeps_the_operator_s_logo(conn: sqlite3.Connection) -> None:
    """재정규화는 몇 번을 눌러도 회사 행을 덮지 않는다."""
    insert_normalized(conn, add_raw(conn, "삼성SDS", 1), [])
    companies.set_logo_url(conn, "삼성SDS", "https://cdn.test/sds.png")

    renormalize(conn, BackfillProgress())
    renormalize(conn, BackfillProgress())

    assert ("삼성SDS", "삼성전자", "https://cdn.test/sds.png") in names(conn)
