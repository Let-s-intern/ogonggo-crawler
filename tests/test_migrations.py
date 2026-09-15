"""실제 `migrations/` 를 임시 DB 에 적용·역적용해 스키마를 확인한다."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app import db
from app.normalize.engine import load_rules
from app.normalize.rules import NORMALIZED_FIELDS

# .claude/docs/data-model.md 의 컬럼. 문서에 없는 컬럼은 늘리지 않는다
EXPECTED_COLUMNS = {
    "crawlers": {
        "id",
        "name",
        "list_url",
        "detail_url",
        "selectors_json",
        "list_mode",
        "detail_mode",
        "api_config_json",
        "status",
        "default_company",
        "created_at",
    },
    "workflows": {
        "id",
        "crawler_id",
        "name",
        "interval_minutes",
        "status",
        "success_count",
        "fail_count",
        "last_run_at",
        "auto_stop_threshold",
    },
    "crawl_runs": {
        "id",
        "workflow_id",
        "crawler_id",
        "started_at",
        "finished_at",
        "status",
        "success_count",
        "new_count",
        "fail_count",
        "skipped_count",
        "error_class",
        "error_message",
        "trigger",
    },
    "raw_jobs": {
        "id",
        "workflow_id",
        "source_url",
        "raw_data_json",
        "content_hash",
        "crawled_at",
    },
    # 0030 이 만든 이력. 원문 다시 수집이 갈아 끼우기 전 raw_jobs 값을 그대로 남긴다
    "raw_job_history": {
        "id",
        "raw_job_id",
        "run_id",
        "raw_data_json",
        "content_hash",
        "crawled_at",
        "replaced_at",
    },
    # 0031 이 이름을 오공고(Spring) Job 엔티티의 칼럼 이름에 맞췄다
    "normalized_jobs": {
        "id",
        "raw_job_id",
        "company_name",
        "title",
        "recruitment_end_at",
        "body",
        "qualifications",
        "source_url",
        "normalized_at",
        "delivered_at",
        # 0011 이 더한 열 칸에서 0016 이 셋을 뺀 나머지
        "recruitment_start_at",
        "employment_type",
        "experience_type",
        "region",
        "responsibilities",
        "preferred_qualifications",
        "hiring_process",
        "recruitment_notice",
        # 0018 이 더한 모회사. 크롤러가 아는 값을 옮기는 칸이라 규칙도 보정도 걸리지 않는다
        "parent_company_name",
        # 0025 가 더한 직무 분류. job_taxonomy 의 대분류(직군)·소분류(직무) 이름을 옮겨 담는다
        "job_field",
        "job_role",
        # 0028 이 더한 다섯 칸. 오공고가 받는 칸이고 분류가 채운다
        "company_and_team_introduction",
        "compensation",
        "benefits",
        "education_level",
        "recruitment_headcount",
        # 0029 가 더한 번호. 수집 건 하나를 나눈 몇 번째 공고인지다
        "part",
        # 0033 이 더한 칸. 분류가 채우는 셋과 정규화가 마감일에서 정하는 둘
        "experience_min_years",
        "closes_when_filled",
        "application_method",
        "recruitment_type",
        "auto_close_enabled",
        # 0034 가 더한 산업. 산업 분류표에서 고른 이름이다
        "industry",
        # 0035 가 더한 대표 이미지. 회사 로고, 없으면 og:image 다
        "cover_image_url",
    },
    "normalization_rules": {
        "id",
        "field_name",
        "rule_type",
        "rule_config_json",
        "priority",
        "enabled",
        "note",
    },
    "app_settings": {
        "key",
        "value",
        "updated_at",
    },
    "job_field_overrides": {
        "id",
        "raw_job_id",
        "part",
        "field_name",
        "value",
        "created_at",
        "updated_at",
    },
    # 0020 이 만든 회사 표. 공고와 외래키로 잇지 않고 회사명으로 잇는다
    "companies": {
        "id",
        "name",
        "parent_name",
        "logo_url",
        "created_at",
        "updated_at",
    },
    "crawl_run_failures": {
        "id",
        "run_id",
        "reason",
        "title",
        "source_url",
        "message",
        "created_at",
    },
    # 0021 이 만든 부가 워크플로우 표. 크롤 `workflows` 와 합치지 않는다
    "side_workflows": {
        "id",
        "kind",
        "name",
        "status",
        "trigger_kind",
        "interval_minutes",
        "target_scope",
        "target_days",
        "batch_limit",
        "last_run_at",
        "created_at",
    },
    # 0021 이 만든 부가 실행 기록. 토큰 수는 `llm_calls` 가 세므로 여기 없다
    "side_runs": {
        "id",
        "side_workflow_id",
        "trigger",
        "started_at",
        "finished_at",
        "status",
        "target_count",
        "processed_count",
        "failed_count",
        "note",
        "error_message",
    },
    # 0023 이 만든 제안 표. 값이 있는 칸에 모델이 낸 다른 값이다. 정규화는 이 표를 읽지 않는다
    "job_field_suggestions": {
        "id",
        "raw_job_id",
        "part",
        "field_name",
        "value",
        "reason",
        "created_at",
    },
    # 0024 가 만든 직무 분류 체계. parent_id 가 NULL 이면 대분류다
    "job_taxonomy": {
        "id",
        "parent_id",
        "name",
        "sort_order",
        "enabled",
        "note",
        "created_at",
        "updated_at",
    },
    # 0032 가 만든 분류 AI 규칙의 판. 저장마다 한 행이고 고치거나 지우지 않는다
    "classify_rule_versions": {
        "id",
        "rules_json",
        "note",
        "created_at",
    },
    # 0036 이 만든 오공고 전송 기록. 원문 주소마다 한 행이다
    "spring_deliveries": {
        "source_url",
        "spring_job_id",
        "status",
        "attempts",
        "last_error",
        "sent_at",
        "created_at",
        "updated_at",
    },
    # 0034 가 만든 산업 분류표. 한 단계이고 지우지 않고 끈다
    "industries": {
        "id",
        "name",
        "sort_order",
        "enabled",
        "note",
        "created_at",
        "updated_at",
    },
}

# 사람이 고칠 수 있는 필드. `source_url` 과 `delivered_at` 은 여기에 없다
OVERRIDABLE = [
    "company_name",
    "title",
    "department",
    "recruitment_end_at",
    "body",
    "qualifications",
]

EXPECTED_INDEXES = {
    "idx_raw_jobs_content_hash",
    "idx_normalized_jobs_normalized_at",
    "idx_crawl_run_failures_run_id",
}

# 지금까지의 마이그레이션. 전부 역적용해야 테이블이 사라진다
ALL_VERSIONS = [
    "0001",
    "0002",
    "0003",
    "0004",
    "0005",
    "0006",
    "0007",
    "0008",
    "0009",
    "0010",
    "0011",
    "0012",
    "0013",
    "0014",
    "0015",
    "0016",
    "0017",
    "0018",
    "0019",
    "0020",
    "0021",
    "0022",
    "0023",
    "0024",
    "0025",
    "0026",
    "0027",
    "0028",
    "0029",
    "0030",
    "0031",
    "0032",
    "0033",
    "0034",
    "0035",
    "0036",
    "0037",
]


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "schema.db")
    yield connection
    connection.close()


def _names(connection: sqlite3.Connection, kind: str) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = ? AND name NOT LIKE 'sqlite_%'", (kind,)
    ).fetchall()
    return {row["name"] for row in rows}


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def _up_to_0030(connection: sqlite3.Connection) -> None:
    """0031 이 칸 이름을 오공고 이름으로 바꾸기 직전까지 올린다.

    그보다 앞선 마이그레이션을 보는 검사는 그때의 이름(`company`, `deadline` 등)으로 값을 넣고
    읽는다. 끝까지 올리면 0031 이 이름을 바꿔 그 검사가 0031 탓으로 깨진다.
    """
    db.migrate_up(connection)
    db.migrate_down(connection, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0031"))


def _down_to_before(connection: sqlite3.Connection, version: str) -> None:
    """`version` 과 그 뒤를 전부 되돌린다. 지금 몇 번까지 올라와 있든 걸음 수를 맞춘다."""
    db.migrate_down(
        connection, steps=len(db.applied_versions(connection)) - ALL_VERSIONS.index(version)
    )


def test_initial_migration_is_the_first_version() -> None:
    migrations = db.load_migrations()

    assert migrations[0].version == "0001"
    assert migrations[0].name == "initial_schema"
    assert [migration.version for migration in migrations] == ALL_VERSIONS


def test_crawl_runs_holds_a_test_run_without_a_workflow(conn: sqlite3.Connection) -> None:
    """승격 전 크롤러의 1회 실행도 행을 남긴다. 어디에도 안 걸린 실행은 막는다."""
    db.migrate_up(conn)
    conn.execute(
        "INSERT INTO crawlers (name, list_url) VALUES (?, ?)", ("테스트", "https://example.test")
    )

    conn.execute("INSERT INTO crawl_runs (crawler_id) VALUES (1)")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO crawl_runs (workflow_id, crawler_id) VALUES (NULL, NULL)")


def test_down_keeps_workflow_runs_and_drops_test_runs(conn: sqlite3.Connection) -> None:
    """0001 스키마는 workflow_id 가 NULL 인 행을 담지 못한다. 역적용은 그 행을 버린다."""
    db.migrate_up(conn)
    conn.execute(
        "INSERT INTO crawlers (name, list_url) VALUES (?, ?)", ("테스트", "https://example.test")
    )
    conn.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '워크플로우')")
    conn.execute("INSERT INTO crawl_runs (workflow_id) VALUES (1)")
    conn.execute("INSERT INTO crawl_runs (crawler_id) VALUES (1)")

    # 0002 까지 되돌린다. 뒤에 붙은 마이그레이션 수만큼 steps 가 늘어난다
    db.migrate_down(conn, steps=len(ALL_VERSIONS) - 1)

    rows = conn.execute("SELECT workflow_id FROM crawl_runs").fetchall()
    assert [row["workflow_id"] for row in rows] == [1]


def test_up_creates_the_declared_tables(conn: sqlite3.Connection) -> None:
    db.migrate_up(conn)

    tables = _names(conn, "table")
    assert set(EXPECTED_COLUMNS) <= tables


def test_up_creates_the_two_indexes(conn: sqlite3.Connection) -> None:
    db.migrate_up(conn)

    assert EXPECTED_INDEXES <= _names(conn, "index")


@pytest.mark.parametrize("table", sorted(EXPECTED_COLUMNS))
def test_columns_match_the_data_model(conn: sqlite3.Connection, table: str) -> None:
    db.migrate_up(conn)

    assert _columns(conn, table) == EXPECTED_COLUMNS[table]


def test_company_columns_start_empty_and_hold_the_two_sources(conn: sqlite3.Connection) -> None:
    """0004 는 컬럼을 더하기만 한다. 기존 행은 NULL 로 남고, 그 NULL 이 "안 적었다" 는 뜻이다."""
    db.migrate_up(conn)
    conn.execute(
        "INSERT INTO crawlers (name, list_url) VALUES (?, ?)", ("이전 행", "https://example.test")
    )

    row = conn.execute("SELECT default_company FROM crawlers WHERE id = 1").fetchone()
    assert row["default_company"] is None

    conn.execute("UPDATE crawlers SET default_company = ? WHERE id = 1", ("삼성전기",))
    saved = conn.execute("SELECT default_company FROM crawlers WHERE id = 1").fetchone()
    assert saved["default_company"] == "삼성전기"


def test_company_source_rejects_a_value_outside_the_two(conn: sqlite3.Connection) -> None:
    """0019 가 지우기 전까지의 CHECK. 되살린 열도 같은 두 값만 받아야 한다."""
    _at_0018(conn)
    conn.execute(
        "INSERT INTO crawlers (name, list_url) VALUES (?, ?)", ("테스트", "https://example.test")
    )
    conn.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '워크플로우')")
    conn.execute(
        """
        INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash)
        VALUES (1, 'https://example.test/1', '{}', 'hash')
        """
    )

    conn.execute(
        """
        INSERT INTO normalized_jobs (raw_job_id, source_url, company, company_source)
        VALUES (1, 'https://example.test/1', '삼성SDS', 'parsed')
        """
    )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO normalized_jobs (raw_job_id, source_url, company_source)
            VALUES (1, 'https://example.test/1', '운영자')
            """
        )


def test_company_down_removes_only_the_two_columns(conn: sqlite3.Connection) -> None:
    """역적용은 0004 가 더한 두 컬럼만 지운다. 나머지 컬럼은 그대로다."""
    db.migrate_up(conn)

    # 0004 까지 내려가려면 그 뒤에 붙은 것들을 먼저 되돌려야 한다. 개수를 세어 구한다 —
    # 마이그레이션이 하나 붙을 때마다 이 숫자를 손으로 고치면 언젠가 잊는다
    db.migrate_down(conn, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0004"))

    assert "default_company" not in _columns(conn, "crawlers")
    assert "company_source" not in _columns(conn, "normalized_jobs")
    assert "company" in _columns(conn, "normalized_jobs")


def test_a_new_company_row_starts_without_a_logo(conn: sqlite3.Connection) -> None:
    """0020 이 만드는 행은 이름만 있다. 로고를 채우는 것은 운영자다."""
    db.migrate_up(conn)

    conn.execute("INSERT INTO companies (name) VALUES ('삼성SDS')")

    row = conn.execute("SELECT * FROM companies WHERE name = '삼성SDS'").fetchone()
    assert (row["parent_name"], row["logo_url"]) == (None, None)
    assert row["created_at"] and row["updated_at"]


def test_the_same_company_name_cannot_be_stored_twice(conn: sqlite3.Connection) -> None:
    """로고를 공고에 잇는 값이 이름이다. 같은 이름이 두 행이면 어느 로고가 붙을지 정할 수 없다."""
    db.migrate_up(conn)
    conn.execute("INSERT INTO companies (name) VALUES ('삼성SDS')")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO companies (name, logo_url) VALUES ('삼성SDS', 'https://cdn.test/a.png')"
        )

    conn.execute("INSERT INTO companies (name) VALUES ('삼성전기')")
    assert conn.execute("SELECT count(*) AS n FROM companies").fetchone()["n"] == 2


def test_companies_down_removes_only_its_own_table(conn: sqlite3.Connection) -> None:
    """역적용은 0020 이 만든 표만 지운다. 수집 데이터는 그대로다."""
    db.migrate_up(conn)
    _seed_raw_job(conn)
    conn.execute("INSERT INTO companies (name) VALUES ('삼성SDS')")

    db.migrate_down(conn, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0020"))

    assert "companies" not in _names(conn, "table")
    assert conn.execute("SELECT count(*) AS n FROM raw_jobs").fetchone()["n"] == 1


def test_foreign_key_is_enforced(conn: sqlite3.Connection) -> None:
    db.migrate_up(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO workflows (crawler_id, name) VALUES (?, ?)", (999, "없는 크롤러"))


def test_status_check_constraints(conn: sqlite3.Connection) -> None:
    db.migrate_up(conn)
    conn.execute(
        "INSERT INTO crawlers (name, list_url) VALUES (?, ?)", ("테스트", "https://example.com")
    )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE crawlers SET status = 'unknown' WHERE id = 1")


def test_down_removes_the_tables_and_indexes(conn: sqlite3.Connection) -> None:
    db.migrate_up(conn)

    assert db.migrate_down(conn, steps=len(ALL_VERSIONS)) == list(reversed(ALL_VERSIONS))
    assert not set(EXPECTED_COLUMNS) & _names(conn, "table")
    assert not EXPECTED_INDEXES & _names(conn, "index")
    assert db.applied_versions(conn) == []


def test_up_after_down_restores_the_same_schema(conn: sqlite3.Connection) -> None:
    db.migrate_up(conn)
    before = _names(conn, "table") | _names(conn, "index")
    db.migrate_down(conn, steps=len(ALL_VERSIONS))

    db.migrate_up(conn)

    assert _names(conn, "table") | _names(conn, "index") == before
    assert db.applied_versions(conn) == ALL_VERSIONS


def _seed_raw_job(connection: sqlite3.Connection) -> None:
    """보정이 매달릴 `raw_jobs` 한 행. 크롤러와 워크플로우까지 있어야 외래키가 선다."""
    connection.execute(
        "INSERT INTO crawlers (name, list_url) VALUES (?, ?)", ("테스트", "https://example.test")
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '워크플로우')")
    connection.execute(
        """
        INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash)
        VALUES (1, 'https://example.test/1', '{}', 'hash')
        """
    )


def test_one_override_per_field_of_one_job(conn: sqlite3.Connection) -> None:
    """같은 공고의 같은 필드에 보정이 둘이면 어느 쪽이 사람의 뜻인지 알 수 없다."""
    db.migrate_up(conn)
    _seed_raw_job(conn)
    conn.execute(
        """
        INSERT INTO job_field_overrides (raw_job_id, field_name, value)
        VALUES (1, 'title', '고침')
        """
    )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO job_field_overrides (raw_job_id, field_name, value)
            VALUES (1, 'title', '또 고침')
            """
        )

    conn.execute(
        """
        INSERT INTO job_field_overrides (raw_job_id, field_name, value)
        VALUES (1, 'company_name', '회사')
        """
    )
    assert conn.execute("SELECT count(*) AS n FROM job_field_overrides").fetchone()["n"] == 2


@pytest.mark.parametrize("field_name", OVERRIDABLE)
def test_override_accepts_every_correctable_field(
    conn: sqlite3.Connection, field_name: str
) -> None:
    db.migrate_up(conn)
    _seed_raw_job(conn)

    conn.execute(
        "INSERT INTO job_field_overrides (raw_job_id, field_name, value) VALUES (1, ?, ?)",
        (field_name, "사람이 고친 값"),
    )

    row = conn.execute("SELECT field_name, value FROM job_field_overrides").fetchone()
    assert (row["field_name"], row["value"]) == (field_name, "사람이 고친 값")


@pytest.mark.parametrize("field_name", ["source_url", "delivered_at", "normalized_at", "id"])
def test_override_rejects_a_field_outside_the_allowlist(
    conn: sqlite3.Connection, field_name: str
) -> None:
    """`source_url` 은 공고의 신원이고, `delivered_at` 은 제공 API 만 쓴다."""
    db.migrate_up(conn)
    _seed_raw_job(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO job_field_overrides (raw_job_id, field_name, value) VALUES (1, ?, ?)",
            (field_name, "값"),
        )


def test_override_needs_an_existing_raw_job(conn: sqlite3.Connection) -> None:
    """보정은 `normalized_jobs` 가 아니라 `raw_jobs` 에 매달린다. 없는 건에는 달 수 없다."""
    db.migrate_up(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO job_field_overrides (raw_job_id, field_name, value)
            VALUES (99, 'title', '값')
            """
        )


def test_override_down_removes_only_its_own_table(conn: sqlite3.Connection) -> None:
    """역적용은 0005 가 만든 테이블만 지운다. 수집·정규화 데이터는 그대로다."""
    db.migrate_up(conn)
    _seed_raw_job(conn)
    conn.execute(
        """
        INSERT INTO job_field_overrides (raw_job_id, field_name, value)
        VALUES (1, 'title', '사람이 고친 값')
        """
    )

    db.migrate_down(conn, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0005"))

    assert "job_field_overrides" not in _names(conn, "table")
    assert conn.execute("SELECT count(*) AS n FROM raw_jobs").fetchone()["n"] == 1


def _at_0007(connection: sqlite3.Connection) -> None:
    """0008 직전 상태로 만든다. `crawlers` 에 `render_mode` 하나만 있는 스키마다."""
    db.migrate_up(connection)
    db.migrate_down(connection, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0008"))
    assert "render_mode" in _columns(connection, "crawlers")


def test_collect_modes_copy_the_previous_render_mode(conn: sqlite3.Connection) -> None:
    """0008 은 값을 옮기기만 한다. 렌더로 돌던 크롤러가 적용 후 정적으로 떨어지면 안 된다."""
    _at_0007(conn)
    conn.execute(
        "INSERT INTO crawlers (name, list_url, render_mode) VALUES ('렌더', ?, 'playwright')",
        ("https://example.test",),
    )
    conn.execute(
        "INSERT INTO crawlers (name, list_url, render_mode) VALUES ('정적', ?, 'static')",
        ("https://example.test/2",),
    )

    db.migrate_up(conn)

    rows = conn.execute(
        "SELECT name, list_mode, detail_mode, api_config_json FROM crawlers ORDER BY id"
    ).fetchall()
    assert [(row["list_mode"], row["detail_mode"]) for row in rows] == [
        ("playwright", "playwright"),
        ("static", "static"),
    ]
    # 쓰지 않는 크롤러의 API 설정은 비어 있다. 빈 객체를 넣으면 "설정했다" 와 구분되지 않는다
    assert [row["api_config_json"] for row in rows] == [None, None]
    assert "render_mode" not in _columns(conn, "crawlers")


@pytest.mark.parametrize("column", ["list_mode", "detail_mode"])
def test_collect_mode_rejects_a_value_outside_the_three(
    conn: sqlite3.Connection, column: str
) -> None:
    db.migrate_up(conn)
    conn.execute(
        "INSERT INTO crawlers (name, list_url) VALUES (?, ?)", ("테스트", "https://example.test")
    )

    conn.execute(f"UPDATE crawlers SET {column} = 'api' WHERE id = 1")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"UPDATE crawlers SET {column} = 'selenium' WHERE id = 1")


def test_collect_modes_down_restores_render_mode(conn: sqlite3.Connection) -> None:
    """역적용은 `list_mode` 를 되돌린다. `api` 는 담을 자리가 없어 정적으로 내려온다."""
    db.migrate_up(conn)
    conn.execute(
        """
        INSERT INTO crawlers (name, list_url, list_mode, detail_mode, api_config_json)
        VALUES ('API', ?, 'api', 'playwright', '{}')
        """,
        ("https://example.test",),
    )
    conn.execute(
        """
        INSERT INTO crawlers (name, list_url, list_mode, detail_mode)
        VALUES ('렌더', ?, 'playwright', 'playwright')
        """,
        ("https://example.test/2",),
    )

    db.migrate_down(conn, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0008"))

    rows = conn.execute("SELECT name, render_mode FROM crawlers ORDER BY id").fetchall()
    assert [row["render_mode"] for row in rows] == ["static", "playwright"]
    assert "list_mode" not in _columns(conn, "crawlers")


def _rule(conn: sqlite3.Connection, rule_type: str, priority: int = 0) -> None:
    conn.execute(
        """
        INSERT INTO normalization_rules (field_name, rule_type, rule_config_json, priority, note)
        VALUES ('body', ?, '{}', ?, '메모')
        """,
        (rule_type, priority),
    )


def test_html_text_rule_type_is_allowed(conn: sqlite3.Connection) -> None:
    """0009 뒤에는 `html_text` 가 저장된다. 나머지 네 타입도 그대로다."""
    db.migrate_up(conn)

    for index, rule_type in enumerate(("mapping", "regex", "trim", "date_parse", "html_text")):
        _rule(conn, rule_type, priority=index)

    with pytest.raises(sqlite3.IntegrityError):
        _rule(conn, "uppercase")


# 이 파일이 직접 넣은 규칙만 고르는 조건. 0016 의 역적용이 `department` 규칙 둘을 되살리므로
# (`migrations/0016_drop_department_category_headcount.sql`), 0009 를 보는 검사는 자기가 넣은
# 행만 세야 무엇을 보고 있는지가 흐려지지 않는다
_OWN_RULES = "WHERE note = '메모'"


def test_html_text_migration_keeps_the_rules_it_did_not_add(conn: sqlite3.Connection) -> None:
    """컬럼을 갈아 끼우는 동안 기존 규칙의 id 와 메모가 그대로 남는다."""
    db.migrate_up(conn)
    db.migrate_down(conn, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0009"))
    _rule(conn, "trim", priority=7)
    before = conn.execute(
        f"SELECT id, rule_type, priority, note FROM normalization_rules {_OWN_RULES}"
    ).fetchall()

    db.migrate_up(conn)

    after = conn.execute(
        f"SELECT id, rule_type, priority, note FROM normalization_rules {_OWN_RULES}"
    ).fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]


def test_html_text_down_drops_only_its_own_rules(conn: sqlite3.Connection) -> None:
    """역적용은 옛 CHECK 에 담을 수 없는 `html_text` 행만 지운다. 나머지는 남는다."""
    db.migrate_up(conn)
    _rule(conn, "trim", priority=0)
    _rule(conn, "html_text", priority=5)

    db.migrate_down(conn, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0009"))

    rows = conn.execute(f"SELECT rule_type FROM normalization_rules {_OWN_RULES}").fetchall()
    assert [row["rule_type"] for row in rows] == ["trim"]


def _at_0009(connection: sqlite3.Connection) -> None:
    """0010 직전 상태로 만든다. `crawl_runs` 에 `skipped_count` 가 아직 없는 스키마다."""
    db.migrate_up(connection)
    db.migrate_down(connection, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0010"))
    assert "skipped_count" not in _columns(connection, "crawl_runs")


def _seed_run(connection: sqlite3.Connection) -> None:
    """실행 기록 한 행. 크롤러와 워크플로우까지 있어야 외래키가 선다."""
    connection.execute(
        "INSERT INTO crawlers (name, list_url) VALUES (?, ?)", ("테스트", "https://example.test")
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '워크플로우')")
    connection.execute(
        """
        INSERT INTO crawl_runs (workflow_id, status, success_count, new_count, fail_count)
        VALUES (1, 'success', 5, 2, 1)
        """
    )


def test_skipped_count_keeps_existing_runs_and_starts_at_zero(conn: sqlite3.Connection) -> None:
    """0010 은 컬럼을 더하기만 한다. 적용 전 실행 기록이 그대로 남고 새 열은 0 이다."""
    _at_0009(conn)
    _seed_run(conn)
    before = conn.execute(
        "SELECT id, status, success_count, new_count, fail_count FROM crawl_runs"
    ).fetchall()

    db.migrate_up(conn)

    after = conn.execute(
        """
        SELECT id, status, success_count, new_count, fail_count, skipped_count
        FROM crawl_runs
        """
    ).fetchall()
    assert [tuple(row)[:5] for row in after] == [tuple(row) for row in before]
    assert [row["skipped_count"] for row in after] == [0]


def test_skipped_count_is_counted_apart_from_fail_count(conn: sqlite3.Connection) -> None:
    """건너뜀과 실패는 다른 열이다. 합치면 전부 걸러진 사이트가 정상 실행으로 보인다."""
    db.migrate_up(conn)
    _seed_run(conn)

    conn.execute("UPDATE crawl_runs SET skipped_count = 83 WHERE id = 1")

    row = conn.execute("SELECT fail_count, skipped_count FROM crawl_runs WHERE id = 1").fetchone()
    assert (row["fail_count"], row["skipped_count"]) == (1, 83)


def test_skipped_count_down_removes_only_its_own_column(conn: sqlite3.Connection) -> None:
    """역적용은 0010 이 더한 열만 지운다. 실행 기록 자체는 그대로 남는다."""
    db.migrate_up(conn)
    _seed_run(conn)
    conn.execute("UPDATE crawl_runs SET skipped_count = 7 WHERE id = 1")

    db.migrate_down(conn, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0010"))

    assert "skipped_count" not in _columns(conn, "crawl_runs")
    row = conn.execute("SELECT fail_count, success_count FROM crawl_runs WHERE id = 1").fetchone()
    assert (row["fail_count"], row["success_count"]) == (1, 5)


def _seed_failure(connection: sqlite3.Connection, reason: str = "detail_empty") -> None:
    connection.execute(
        """
        INSERT INTO crawl_run_failures (run_id, reason, title, source_url, message)
        VALUES (1, ?, '2026 상반기 신입 채용', 'https://example.test/list', '본문이 비었다')
        """,
        (reason,),
    )


def test_run_failures_are_deleted_with_the_run_they_explain(conn: sqlite3.Connection) -> None:
    """실패 목록은 그 실행을 설명하는 기록이다. 실행이 지워지면 같이 지워진다."""
    db.migrate_up(conn)
    _seed_run(conn)
    _seed_failure(conn)
    _seed_failure(conn, reason="detail_unreachable")

    assert conn.execute("SELECT count(*) AS n FROM crawl_run_failures").fetchone()["n"] == 2

    conn.execute("DELETE FROM crawl_runs WHERE id = 1")

    assert conn.execute("SELECT count(*) AS n FROM crawl_run_failures").fetchone()["n"] == 0


def test_run_failure_needs_an_existing_run(conn: sqlite3.Connection) -> None:
    """어느 실행에도 걸리지 않은 실패 기록은 아무도 추적하지 못한다."""
    db.migrate_up(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO crawl_run_failures (run_id, reason) VALUES (99, 'detail_empty')")


def test_run_failure_keeps_the_posting_it_missed(conn: sqlite3.Connection) -> None:
    """건수만으로는 고칠 수 없다. 제목과 목록에서 읽은 주소가 같이 남는다."""
    db.migrate_up(conn)
    _seed_run(conn)
    _seed_failure(conn)

    row = conn.execute(
        "SELECT run_id, reason, title, source_url, message, created_at FROM crawl_run_failures"
    ).fetchone()
    assert (row["run_id"], row["reason"]) == (1, "detail_empty")
    assert (row["title"], row["source_url"]) == (
        "2026 상반기 신입 채용",
        "https://example.test/list",
    )
    assert row["message"] == "본문이 비었다"
    assert row["created_at"]


def test_run_failure_down_drops_only_its_own_table(conn: sqlite3.Connection) -> None:
    """역적용은 0010 이 만든 표만 지운다. 실행 기록과 수집 데이터는 그대로다."""
    db.migrate_up(conn)
    _seed_run(conn)
    _seed_failure(conn)

    db.migrate_down(conn, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0010"))

    assert "crawl_run_failures" not in _names(conn, "table")
    assert "idx_crawl_run_failures_run_id" not in _names(conn, "index")
    assert conn.execute("SELECT count(*) AS n FROM crawl_runs").fetchone()["n"] == 1


@pytest.mark.parametrize(
    "error_class",
    ["transport", "selector_miss", "parse", "list_empty", "detail_unreachable", "detail_empty"],
)
def test_run_error_class_holds_every_failure_reason(
    conn: sqlite3.Connection, error_class: str
) -> None:
    """`crawl_runs.error_class` 는 `app/crawler/failures.py` 의 `ERROR_CLASSES` 와 같은 값이다."""
    db.migrate_up(conn)
    _seed_run(conn)

    conn.execute("UPDATE crawl_runs SET error_class = ? WHERE id = 1", (error_class,))

    row = conn.execute("SELECT error_class FROM crawl_runs WHERE id = 1").fetchone()
    assert row["error_class"] == error_class


def test_run_error_class_rejects_a_reason_outside_the_six(conn: sqlite3.Connection) -> None:
    db.migrate_up(conn)
    _seed_run(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE crawl_runs SET error_class = 'detail_missing' WHERE id = 1")


def test_error_class_migration_keeps_the_runs_it_did_not_add(conn: sqlite3.Connection) -> None:
    """컬럼을 갈아 끼우는 동안 기존 실행의 id 와 카운트, 옛 분류가 그대로 남는다."""
    _at_0009(conn)
    _seed_run(conn)
    conn.execute("UPDATE crawl_runs SET error_class = 'selector_miss' WHERE id = 1")
    before = conn.execute(
        "SELECT id, status, success_count, fail_count, error_class FROM crawl_runs"
    ).fetchall()

    db.migrate_up(conn)

    after = conn.execute(
        "SELECT id, status, success_count, fail_count, error_class FROM crawl_runs"
    ).fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]


def test_error_class_down_keeps_the_run_and_clears_only_the_new_reason(
    conn: sqlite3.Connection,
) -> None:
    """역적용은 옛 CHECK 에 담을 수 없는 분류만 비운다. 실행 기록과 사유 문구는 남는다."""
    db.migrate_up(conn)
    _seed_run(conn)
    conn.execute(
        "UPDATE crawl_runs SET error_class = 'detail_empty', error_message = '본문이 비었다'"
    )

    db.migrate_down(conn, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0010"))

    row = conn.execute(
        "SELECT id, error_class, error_message, fail_count FROM crawl_runs"
    ).fetchone()
    assert (row["id"], row["error_class"]) == (1, None)
    assert (row["error_message"], row["fail_count"]) == ("본문이 비었다", 1)


# 0011 이 더한 칸. `migrations/0011_split_body_columns.sql` 의 표와 같은 목록이어야 한다
SPLIT_BODY_COLUMNS = [
    "start_date",
    "job_category",
    "employment_type",
    "career_level",
    "work_location",
    "headcount",
    "duties",
    "preferred",
    "hiring_process",
    "etc_info",
]

# 0011 이 더한 열 칸 중 0016 이 지운 둘. 전부 적용한 자리에서는 이 둘이 없으므로, 0011 이
# 무엇을 더했는지 보는 검사는 살아남은 여덟만 센다
SPLIT_BODY_KEPT = [name for name in SPLIT_BODY_COLUMNS if name not in ("job_category", "headcount")]

# 0011 이 건드리지 않는 칸. 소비 측이 읽던 것이라 이름도 뜻도 그대로다.
# `department` 는 0016 이 지웠다
KEPT_COLUMNS = "company, title, deadline, body, requirements, source_url"


def _at_0010(connection: sqlite3.Connection) -> None:
    """0011 직전 상태로 만든다. `normalized_jobs` 가 아직 여섯 칸인 스키마다."""
    db.migrate_up(connection)
    db.migrate_down(connection, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0011"))
    assert "start_date" not in _columns(connection, "normalized_jobs")


def _seed_normalized(connection: sqlite3.Connection) -> None:
    """정규화된 공고 한 행. 남은 칸에 값이 다 들어 있다."""
    _seed_raw_job(connection)
    connection.execute(
        """
        INSERT INTO normalized_jobs
               (raw_job_id, company, title, deadline, body, requirements, source_url)
        VALUES (1, '한화생명', '마케팅 기획', '2026-08-25', '본문', '자격요건',
                'https://example.test/1')
        """
    )


def test_split_body_only_adds_columns_and_keeps_the_existing_values(
    conn: sqlite3.Connection,
) -> None:
    """0011 은 더하기만 한다. 적용 전 공고의 여섯 칸이 글자 하나까지 그대로 남는다."""
    _at_0010(conn)
    _seed_normalized(conn)
    before = conn.execute(f"SELECT id, {KEPT_COLUMNS} FROM normalized_jobs").fetchall()

    _up_to_0030(conn)

    after = conn.execute(f"SELECT id, {KEPT_COLUMNS} FROM normalized_jobs").fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]
    assert set(SPLIT_BODY_KEPT) <= _columns(conn, "normalized_jobs")


def test_split_body_leaves_the_new_columns_empty(conn: sqlite3.Connection) -> None:
    """사이트가 주지 않는 칸은 빈 칸이다. 기본값으로 채우지 않는다."""
    _at_0010(conn)
    _seed_normalized(conn)

    _up_to_0030(conn)

    row = conn.execute(f"SELECT {', '.join(SPLIT_BODY_KEPT)} FROM normalized_jobs").fetchone()
    assert [row[name] for name in SPLIT_BODY_KEPT] == [None] * len(SPLIT_BODY_KEPT)


def test_split_body_down_drops_only_the_ten_it_added(conn: sqlite3.Connection) -> None:
    """역적용은 더한 열 칸만 지운다. 공고와 여섯 칸의 값은 그대로다."""
    _up_to_0030(conn)
    _seed_normalized(conn)
    conn.execute("UPDATE normalized_jobs SET work_location = '서울', duties = '기획'")
    before = conn.execute(f"SELECT id, {KEPT_COLUMNS} FROM normalized_jobs").fetchall()

    _down_to_before(conn, "0011")

    after = conn.execute(f"SELECT id, {KEPT_COLUMNS} FROM normalized_jobs").fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]
    assert not set(SPLIT_BODY_COLUMNS) & _columns(conn, "normalized_jobs")


def test_the_override_check_covers_the_new_columns(conn: sqlite3.Connection) -> None:
    """0012 가 넓혔다. 새 칸에 자동으로 뽑은 값이 틀렸을 때 사람이 고칠 수 있어야 한다."""
    db.migrate_up(conn)
    _seed_raw_job(conn)

    for field in NORMALIZED_FIELDS:
        conn.execute(
            "INSERT INTO job_field_overrides (raw_job_id, field_name, value) VALUES (1, ?, ?)",
            (field, "사람이 고친 값"),
        )

    stored = conn.execute("SELECT count(*) AS n FROM job_field_overrides").fetchone()
    assert stored["n"] == len(NORMALIZED_FIELDS)


def test_the_override_check_still_refuses_a_column_that_is_not_normalized(
    conn: sqlite3.Connection,
) -> None:
    """넓어진 것은 `normalized_jobs` 의 칸까지다. `source_url` 은 공고의 신원이라 못 고친다."""
    db.migrate_up(conn)
    _seed_raw_job(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO job_field_overrides (raw_job_id, field_name, value) VALUES (1, ?, ?)",
            ("source_url", "https://example.test/other"),
        )


def test_the_override_migration_keeps_the_rows_it_did_not_add(conn: sqlite3.Connection) -> None:
    """표를 다시 만드는 마이그레이션이다. 있던 보정이 id 까지 그대로 넘어와야 한다."""
    db.migrate_up(conn)
    db.migrate_down(conn, steps=1)
    _seed_raw_job(conn)
    conn.execute(
        "INSERT INTO job_field_overrides (raw_job_id, field_name, value) VALUES (1, ?, ?)",
        ("title", "사람이 고친 제목"),
    )
    before = conn.execute("SELECT id, created_at FROM job_field_overrides").fetchone()

    db.migrate_up(conn)

    after = conn.execute(
        "SELECT id, field_name, value, created_at FROM job_field_overrides"
    ).fetchall()
    assert len(after) == 1
    assert after[0]["id"] == before["id"]
    assert after[0]["value"] == "사람이 고친 제목"
    assert after[0]["created_at"] == before["created_at"]


def test_the_override_down_drops_the_corrections_the_old_check_cannot_hold(
    conn: sqlite3.Connection,
) -> None:
    """되돌리면 새 칸의 보정은 사라진다. 옛 CHECK 에 담을 자리가 없다."""
    _up_to_0030(conn)
    _seed_raw_job(conn)
    conn.executemany(
        "INSERT INTO job_field_overrides (raw_job_id, field_name, value) VALUES (1, ?, ?)",
        [("title", "사람이 고친 제목"), ("work_location", "서울")],
    )

    # 0012 까지 되돌린다
    _down_to_before(conn, "0012")

    rows = conn.execute("SELECT field_name FROM job_field_overrides").fetchall()
    assert [row["field_name"] for row in rows] == ["title"]
    assert conn.execute("SELECT count(*) AS n FROM raw_jobs").fetchone()["n"] == 1


def _rules(connection: sqlite3.Connection) -> list[tuple[str, str, int]]:
    rows = connection.execute(
        "SELECT field_name, rule_type, priority FROM normalization_rules ORDER BY id"
    ).fetchall()
    return [(row["field_name"], row["rule_type"], row["priority"]) for row in rows]


def _at_0015(connection: sqlite3.Connection) -> None:
    """0016 직전 상태로 만든다. 지운 세 칸이 아직 있는 스키마다."""
    db.migrate_up(connection)
    db.migrate_down(connection, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0016"))
    connection.execute("DELETE FROM normalization_rules")


def test_dropped_field_rules_go_before_the_columns(conn: sqlite3.Connection) -> None:
    """0016 은 지운 칸의 규칙을 먼저 지운다. 남기면 `load_rules` 가 정규화 전체를 세운다."""
    _at_0015(conn)
    conn.executemany(
        """
        INSERT INTO normalization_rules (field_name, rule_type, rule_config_json, priority)
        VALUES (?, 'trim', '{}', 0)
        """,
        [("department",), ("job_category",), ("headcount",), ("title",)],
    )

    db.migrate_up(conn)

    # 예외 없이 돌고, 남은 것은 지우지 않은 칸의 규칙뿐이다
    assert [rule.field_name for rule in load_rules(conn)] == ["title"]
    assert _rules(conn) == [("title", "trim", 0)]


def test_dropped_field_rules_come_back_on_down(conn: sqlite3.Connection) -> None:
    """되돌리면 `seeds/normalization-rules.json` 의 `department` 규칙 둘이 되살아난다."""
    db.migrate_up(conn)
    conn.execute("DELETE FROM normalization_rules")

    db.migrate_down(conn, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0016"))

    assert _rules(conn) == [("department", "trim", 0), ("department", "regex", 10)]


# 0016 이 지우는 세 칸
DROPPED_COLUMNS = ["department", "job_category", "headcount"]


def _seed_normalized_with_dropped(connection: sqlite3.Connection) -> None:
    """0016 직전의 공고 한 행. 지워질 세 칸에도 값이 들어 있다."""
    _seed_raw_job(connection)
    connection.execute(
        """
        INSERT INTO normalized_jobs
               (raw_job_id, company, title, deadline, body, requirements, source_url,
                department, job_category, headcount)
        VALUES (1, '한화생명', '마케팅 기획', '2026-08-25', '본문', '자격요건',
                'https://example.test/1', '마케팅본부', '영업', '0명')
        """
    )


def test_dropping_the_three_keeps_the_rows_and_the_other_values(
    conn: sqlite3.Connection,
) -> None:
    """0016 은 칸만 지운다. 공고가 사라지거나 남은 칸의 값이 바뀌면 안 된다."""
    _at_0015(conn)
    _seed_normalized_with_dropped(conn)
    before = conn.execute(f"SELECT id, {KEPT_COLUMNS} FROM normalized_jobs").fetchall()

    _up_to_0030(conn)

    after = conn.execute(f"SELECT id, {KEPT_COLUMNS} FROM normalized_jobs").fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]
    assert not set(DROPPED_COLUMNS) & _columns(conn, "normalized_jobs")


def test_the_three_come_back_empty_on_down(conn: sqlite3.Connection) -> None:
    """되돌리면 칸은 돌아오지만 값은 돌아오지 않는다. 어디에도 옮겨 두지 않았다."""
    _at_0015(conn)
    _seed_normalized_with_dropped(conn)
    db.migrate_up(conn)

    db.migrate_down(conn, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0016"))

    assert set(DROPPED_COLUMNS) <= _columns(conn, "normalized_jobs")
    row = conn.execute(f"SELECT {', '.join(DROPPED_COLUMNS)} FROM normalized_jobs").fetchone()
    assert [row[name] for name in DROPPED_COLUMNS] == [None] * len(DROPPED_COLUMNS)
    assert conn.execute("SELECT count(*) AS n FROM normalized_jobs").fetchone()["n"] == 1


def _at_0016(connection: sqlite3.Connection) -> None:
    """0017 직전 상태로 만든다. 직무 칸이 아직 없는 스키마다."""
    db.migrate_up(connection)
    db.migrate_down(connection, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0017"))


def test_job_role_is_added_to_both_tables(conn: sqlite3.Connection) -> None:
    """분류가 앉히는 자리와 소비 측이 읽는 자리 둘 다에 있어야 값이 끝까지 간다."""
    _at_0016(conn)
    assert "job_role" not in _columns(conn, "normalized_jobs")
    assert "job_role" not in _columns(conn, "job_classifications")

    db.migrate_up(conn)

    assert "job_role" in _columns(conn, "normalized_jobs")
    assert "job_role" in _columns(conn, "job_classifications")


def test_job_role_starts_empty_and_leaves_the_other_values_alone(
    conn: sqlite3.Connection,
) -> None:
    """칸만 더한다. 있던 공고가 사라지거나 남은 칸의 값이 바뀌면 안 된다."""
    _at_0016(conn)
    _seed_raw_job(conn)
    conn.execute(
        """
        INSERT INTO normalized_jobs
               (raw_job_id, company, title, deadline, body, requirements, source_url)
        VALUES (1, '한화생명', '마케팅 기획', '2026-08-25', '본문', '자격요건',
                'https://example.test/1')
        """
    )
    before = conn.execute(f"SELECT id, {KEPT_COLUMNS} FROM normalized_jobs").fetchall()

    _up_to_0030(conn)

    after = conn.execute(f"SELECT id, {KEPT_COLUMNS} FROM normalized_jobs").fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]
    assert conn.execute("SELECT job_role FROM normalized_jobs").fetchone()["job_role"] is None


def test_job_role_can_be_corrected_by_hand(conn: sqlite3.Connection) -> None:
    """자유 텍스트라 틀리게 뽑힐 여지가 가장 큰 칸이다. 고칠 길이 없으면 안 된다."""
    db.migrate_up(conn)
    _seed_raw_job(conn)

    conn.execute(
        "INSERT INTO job_field_overrides (raw_job_id, field_name, value) VALUES (1, ?, ?)",
        ("job_role", "백엔드 개발"),
    )

    row = conn.execute("SELECT field_name, value FROM job_field_overrides").fetchone()
    assert (row["field_name"], row["value"]) == ("job_role", "백엔드 개발")


def test_the_job_role_migration_keeps_the_overrides_it_did_not_add(
    conn: sqlite3.Connection,
) -> None:
    """표를 다시 만드는 마이그레이션이다. 있던 보정이 id 까지 그대로 넘어와야 한다."""
    _at_0016(conn)
    _seed_raw_job(conn)
    conn.execute(
        "INSERT INTO job_field_overrides (raw_job_id, field_name, value) VALUES (1, ?, ?)",
        ("title", "사람이 고친 제목"),
    )
    before = conn.execute("SELECT id, created_at FROM job_field_overrides").fetchone()

    db.migrate_up(conn)

    after = conn.execute(
        "SELECT id, field_name, value, created_at FROM job_field_overrides"
    ).fetchall()
    assert len(after) == 1
    assert after[0]["id"] == before["id"]
    assert after[0]["value"] == "사람이 고친 제목"
    assert after[0]["created_at"] == before["created_at"]


def test_the_job_role_down_drops_the_column_and_its_corrections(
    conn: sqlite3.Connection,
) -> None:
    """되돌리면 직무 값과 직무에 걸린 보정만 사라진다. 나머지는 그대로다."""
    db.migrate_up(conn)
    _seed_raw_job(conn)
    conn.executemany(
        "INSERT INTO job_field_overrides (raw_job_id, field_name, value) VALUES (1, ?, ?)",
        [("title", "사람이 고친 제목"), ("job_role", "백엔드 개발")],
    )

    db.migrate_down(conn, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0017"))

    assert "job_role" not in _columns(conn, "normalized_jobs")
    assert "job_role" not in _columns(conn, "job_classifications")
    rows = conn.execute("SELECT field_name FROM job_field_overrides").fetchall()
    assert [row["field_name"] for row in rows] == ["title"]
    assert conn.execute("SELECT count(*) AS n FROM raw_jobs").fetchone()["n"] == 1


def test_the_job_role_down_keeps_the_corrections_of_the_dropped_columns(
    conn: sqlite3.Connection,
) -> None:
    """0016 이 지운 셋의 보정 행은 되돌릴 때 필요하다. CHECK 를 좁히면 여기서 떨어진다."""
    db.migrate_up(conn)
    _seed_raw_job(conn)
    conn.execute(
        "INSERT INTO job_field_overrides (raw_job_id, field_name, value) VALUES (1, ?, ?)",
        ("department", "사람이 고친 부서"),
    )

    db.migrate_down(conn, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0017"))

    rows = conn.execute("SELECT field_name FROM job_field_overrides").fetchall()
    assert [row["field_name"] for row in rows] == ["department"]


def _at_0017(connection: sqlite3.Connection) -> None:
    """0018 직전 상태로 만든다. 회사명이 아직 한 칸인 스키마다."""
    db.migrate_up(connection)
    db.migrate_down(connection, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0018"))


def test_parent_company_is_added_to_normalized_jobs(conn: sqlite3.Connection) -> None:
    _at_0017(conn)
    assert "parent_company" not in _columns(conn, "normalized_jobs")

    _up_to_0030(conn)

    assert "parent_company" in _columns(conn, "normalized_jobs")
    assert "company" in _columns(conn, "normalized_jobs")


def test_parent_company_starts_empty_and_leaves_the_other_values_alone(
    conn: sqlite3.Connection,
) -> None:
    """칸만 더한다. 값은 재정규화가 넣는다 — 이 마이그레이션에 UPDATE 가 없다."""
    _at_0017(conn)
    _seed_raw_job(conn)
    conn.execute(
        """
        INSERT INTO normalized_jobs
               (raw_job_id, company, title, deadline, body, requirements, source_url)
        VALUES (1, '삼성SDS', '백엔드 개발자', '2026-08-25', '본문', '자격요건',
                'https://example.test/1')
        """
    )
    before = conn.execute(f"SELECT id, {KEPT_COLUMNS} FROM normalized_jobs").fetchall()

    _up_to_0030(conn)

    after = conn.execute(f"SELECT id, {KEPT_COLUMNS} FROM normalized_jobs").fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]
    row = conn.execute("SELECT parent_company FROM normalized_jobs").fetchone()
    assert row["parent_company"] is None


def test_the_parent_company_migration_leaves_the_two_neighbour_tables_alone(
    conn: sqlite3.Connection,
) -> None:
    """분류가 내는 값도 아니고 공고 한 건씩 고칠 값도 아니다. 두 표는 그대로여야 한다.

    0018 하나만 본다 — 끝까지(latest) 올리면 그 뒤의 무관한 마이그레이션(0025 등)이 이
    두 표에 칸을 더한 것까지 0018 탓으로 걸리게 된다.
    """
    _at_0017(conn)
    before_classifications = _columns(conn, "job_classifications")
    before_overrides = _columns(conn, "job_field_overrides")

    db.migrate_up(conn)
    db.migrate_down(conn, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0019"))

    assert _columns(conn, "job_classifications") == before_classifications
    assert _columns(conn, "job_field_overrides") == before_overrides
    _seed_raw_job(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO job_field_overrides (raw_job_id, field_name, value) VALUES (1, ?, ?)",
            ("parent_company", "삼성"),
        )


def test_the_parent_company_down_drops_only_that_column(conn: sqlite3.Connection) -> None:
    """되돌리면 모회사 값만 사라진다. 그 값은 `crawlers` 에 그대로 있어 재정규화로 돌아온다."""
    _up_to_0030(conn)
    _seed_raw_job(conn)
    conn.execute(
        """
        INSERT INTO normalized_jobs (raw_job_id, company, parent_company, title, source_url)
        VALUES (1, '삼성SDS', '삼성', '백엔드 개발자', 'https://example.test/1')
        """
    )

    _down_to_before(conn, "0018")

    assert "parent_company" not in _columns(conn, "normalized_jobs")
    row = conn.execute("SELECT company, title FROM normalized_jobs").fetchone()
    assert (row["company"], row["title"]) == ("삼성SDS", "백엔드 개발자")
    assert conn.execute("SELECT count(*) AS n FROM raw_jobs").fetchone()["n"] == 1


def _at_0018(connection: sqlite3.Connection) -> None:
    """0019 직전 상태로 만든다. 회사명 출처 열이 아직 있는 스키마다."""
    db.migrate_up(connection)
    db.migrate_down(connection, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0019"))


def test_company_source_is_dropped(conn: sqlite3.Connection) -> None:
    """칸 이름이 출처를 말하게 된 뒤로 이 열은 답이 둘이 되게 할 뿐이다."""
    _at_0018(conn)
    assert "company_source" in _columns(conn, "normalized_jobs")

    _up_to_0030(conn)

    assert "company_source" not in _columns(conn, "normalized_jobs")
    # 회사명 두 칸은 그대로다. 지운 것은 출처 열 하나뿐이다
    assert {"company", "parent_company"} <= _columns(conn, "normalized_jobs")


def test_dropping_company_source_keeps_the_rows_and_the_two_company_columns(
    conn: sqlite3.Connection,
) -> None:
    _at_0018(conn)
    _seed_raw_job(conn)
    conn.execute(
        """
        INSERT INTO normalized_jobs
               (raw_job_id, parent_company, company, company_source, title, source_url)
        VALUES (1, '삼성전자', '삼성SDS', 'parsed', '백엔드 개발자', 'https://example.test/1')
        """
    )

    _up_to_0030(conn)

    row = conn.execute("SELECT parent_company, company, title FROM normalized_jobs").fetchone()
    assert (row["parent_company"], row["company"], row["title"]) == (
        "삼성전자",
        "삼성SDS",
        "백엔드 개발자",
    )


def test_the_company_source_down_restores_the_column_empty_with_its_check(
    conn: sqlite3.Connection,
) -> None:
    """컬럼은 돌아오지만 값은 돌아오지 않는다. CHECK 는 같은 모양으로 돌아와야 한다."""
    _up_to_0030(conn)
    _seed_raw_job(conn)
    conn.execute(
        """
        INSERT INTO normalized_jobs (raw_job_id, company, title, source_url)
        VALUES (1, '삼성SDS', '백엔드 개발자', 'https://example.test/1')
        """
    )

    _down_to_before(conn, "0019")

    assert "company_source" in _columns(conn, "normalized_jobs")
    row = conn.execute("SELECT company, company_source FROM normalized_jobs").fetchone()
    assert (row["company"], row["company_source"]) == ("삼성SDS", None)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE normalized_jobs SET company_source = '운영자'")


def _seed_side_workflow(connection: sqlite3.Connection, **overrides: object) -> int:
    """부가 워크플로우 한 행. 값을 주지 않은 칸은 표의 기본값이 채운다."""
    values: dict[str, object] = {
        "kind": "classify",
        "name": "분류",
        "target_scope": "unclassified",
    }
    values.update(overrides)
    columns = ", ".join(values)
    marks = ", ".join("?" * len(values))
    cursor = connection.execute(
        f"INSERT INTO side_workflows ({columns}) VALUES ({marks})", tuple(values.values())
    )
    return int(cursor.lastrowid or 0)


def test_a_new_side_workflow_starts_paused(conn: sqlite3.Connection) -> None:
    """만들자마자 도는 일이 없다. `all` 은 640건이면 약 285만 토큰이다."""
    db.migrate_up(conn)

    _seed_side_workflow(conn)

    row = conn.execute("SELECT * FROM side_workflows WHERE id = 1").fetchone()
    assert row["status"] == "paused"
    assert row["trigger_kind"] == "manual"
    assert (row["batch_limit"], row["target_days"], row["last_run_at"]) == (50, None, None)
    assert row["created_at"]


def test_side_workflows_has_no_token_columns(conn: sqlite3.Connection) -> None:
    """토큰은 `llm_calls` 가 호출마다 센다. 같은 숫자를 두 곳에서 세지 않는다."""
    db.migrate_up(conn)

    assert not [name for name in _columns(conn, "side_workflows") if "token" in name]
    assert not [name for name in _columns(conn, "side_runs") if "token" in name]


@pytest.mark.parametrize(
    ("kind", "scope"),
    [
        ("classify", "unclassified"),
        ("classify", "empty_fields"),
        ("classify", "all"),
        ("deliver", "undelivered"),
        ("deliver", "all"),
    ],
)
def test_each_kind_accepts_its_own_scopes(conn: sqlite3.Connection, kind: str, scope: str) -> None:
    db.migrate_up(conn)

    _seed_side_workflow(conn, kind=kind, target_scope=scope)

    assert conn.execute("SELECT count(*) AS n FROM side_workflows").fetchone()["n"] == 1


@pytest.mark.parametrize(
    ("kind", "scope"),
    [
        # 전달에는 분류할 것이 없고, 분류에는 전달 여부라는 것이 없다
        ("deliver", "unclassified"),
        ("deliver", "empty_fields"),
        ("classify", "undelivered"),
        ("classify", "없는범위"),
    ],
)
def test_a_scope_the_kind_does_not_take_is_rejected(
    conn: sqlite3.Connection, kind: str, scope: str
) -> None:
    """저장할 때 막지 않으면 실행할 때 대상을 못 찾는 것으로 드러난다."""
    db.migrate_up(conn)

    with pytest.raises(sqlite3.IntegrityError):
        _seed_side_workflow(conn, kind=kind, target_scope=scope)


def test_target_days_belongs_to_recent_and_only_to_recent(conn: sqlite3.Connection) -> None:
    """`recent` 에는 일수가 반드시 있고, 그 밖에는 없어야 한다."""
    db.migrate_up(conn)

    _seed_side_workflow(conn, target_scope="recent", target_days=7)

    with pytest.raises(sqlite3.IntegrityError):
        _seed_side_workflow(conn, target_scope="recent")
    with pytest.raises(sqlite3.IntegrityError):
        _seed_side_workflow(conn, target_scope="unclassified", target_days=7)
    with pytest.raises(sqlite3.IntegrityError):
        _seed_side_workflow(conn, target_scope="recent", target_days=0)

    assert conn.execute("SELECT count(*) AS n FROM side_workflows").fetchone()["n"] == 1


def test_side_workflow_rejects_a_batch_limit_below_one(conn: sqlite3.Connection) -> None:
    """위쪽 상한은 `app/classify/batch.py` 의 `MAX_LIMIT` 이 정한다. DB 는 아래만 막는다."""
    db.migrate_up(conn)

    with pytest.raises(sqlite3.IntegrityError):
        _seed_side_workflow(conn, batch_limit=0)

    _seed_side_workflow(conn, batch_limit=1000)
    assert conn.execute("SELECT batch_limit FROM side_workflows").fetchone()["batch_limit"] == 1000


def test_a_side_run_starts_without_a_status(conn: sqlite3.Connection) -> None:
    """시작할 때 행이 생기고 종료 상태는 그때 없다. 기록 없는 실행이 없어야 한다."""
    db.migrate_up(conn)
    _seed_side_workflow(conn)

    conn.execute("INSERT INTO side_runs (side_workflow_id, trigger) VALUES (1, 'manual')")

    row = conn.execute("SELECT * FROM side_runs WHERE id = 1").fetchone()
    assert (row["status"], row["finished_at"]) == (None, None)
    assert (row["target_count"], row["processed_count"], row["failed_count"]) == (0, 0, 0)
    assert row["started_at"]


@pytest.mark.parametrize("status", ["success", "failed", "skipped", "timeout"])
def test_a_side_run_takes_the_four_end_states(conn: sqlite3.Connection, status: str) -> None:
    """`skipped` 는 앞 실행이 돌고 있어 건너뛴 것, `timeout` 은 종료를 적지 못한 것이다."""
    db.migrate_up(conn)
    _seed_side_workflow(conn)
    conn.execute("INSERT INTO side_runs (side_workflow_id, trigger) VALUES (1, 'schedule')")

    conn.execute("UPDATE side_runs SET status = ? WHERE id = 1", (status,))

    assert conn.execute("SELECT status FROM side_runs").fetchone()["status"] == status


def test_side_run_rejects_an_unknown_status_or_trigger(conn: sqlite3.Connection) -> None:
    db.migrate_up(conn)
    _seed_side_workflow(conn)
    conn.execute("INSERT INTO side_runs (side_workflow_id, trigger) VALUES (1, 'after_crawl')")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE side_runs SET status = '끝남' WHERE id = 1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO side_runs (side_workflow_id, trigger) VALUES (1, 'test')")


def test_a_side_run_needs_an_existing_side_workflow(conn: sqlite3.Connection) -> None:
    """어디에도 안 걸린 실행 기록은 누구의 것인지 알 수 없다."""
    db.migrate_up(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO side_runs (side_workflow_id, trigger) VALUES (99, 'manual')")


def test_side_tables_down_removes_only_its_own_two_tables(conn: sqlite3.Connection) -> None:
    """역적용은 0021 이 만든 두 표만 지운다. 크롤 쪽 기록은 그대로다."""
    db.migrate_up(conn)
    _seed_raw_job(conn)
    conn.execute("INSERT INTO crawl_runs (workflow_id) VALUES (1)")
    _seed_side_workflow(conn)
    conn.execute("INSERT INTO side_runs (side_workflow_id, trigger) VALUES (1, 'manual')")

    db.migrate_down(conn, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0021"))

    tables = _names(conn, "table")
    assert "side_workflows" not in tables
    assert "side_runs" not in tables
    assert {"workflows", "crawl_runs", "raw_jobs", "normalized_jobs"} <= tables
    assert conn.execute("SELECT count(*) AS n FROM crawl_runs").fetchone()["n"] == 1
    assert conn.execute("SELECT count(*) AS n FROM raw_jobs").fetchone()["n"] == 1


# 0023 이 대상으로 받는 필드. `app/normalize/rules.py` 의 `NORMALIZED_FIELDS` 와 같은 값이다
def test_job_field_suggestions_accepts_every_normalized_field(conn: sqlite3.Connection) -> None:
    """분류가 채우는 아홉 칸과 수집이 채우는 다섯 칸 전부가 제안 대상이다 (11.1.V)."""
    db.migrate_up(conn)
    _seed_raw_job(conn)

    for field_name in NORMALIZED_FIELDS:
        conn.execute(
            """
            INSERT INTO job_field_suggestions (raw_job_id, field_name, value, reason)
            VALUES (1, ?, '제안 값', '원문과 다르다')
            """,
            (field_name,),
        )

    stored = conn.execute("SELECT count(*) AS n FROM job_field_suggestions").fetchone()
    assert stored["n"] == len(NORMALIZED_FIELDS)


def test_job_field_suggestions_rejects_a_field_outside_the_list(conn: sqlite3.Connection) -> None:
    """`source_url` 은 공고의 신원이라 제안 대상이 아니다 (11.1.V)."""
    db.migrate_up(conn)
    _seed_raw_job(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO job_field_suggestions (raw_job_id, field_name, value, reason)
            VALUES (1, 'source_url', 'https://example.test/other', '다르다')
            """
        )


def test_a_new_suggestion_on_the_same_column_overwrites_the_old_one(
    conn: sqlite3.Connection,
) -> None:
    """같은 칸에 제안이 둘이면 어느 것을 보고 있는지 알 수 없다 (11.1.V)."""
    db.migrate_up(conn)
    _seed_raw_job(conn)

    for value in ("첫 제안", "다음 제안"):
        conn.execute(
            """
            INSERT INTO job_field_suggestions (raw_job_id, field_name, value, reason)
            VALUES (1, 'recruitment_end_at', ?, '원문과 다르다')
            ON CONFLICT (raw_job_id, part, field_name) DO UPDATE
               SET value = excluded.value, reason = excluded.reason,
                   created_at = datetime('now')
            """,
            (value,),
        )

    rows = conn.execute("SELECT value FROM job_field_suggestions").fetchall()
    assert [row["value"] for row in rows] == ["다음 제안"]


def test_job_field_suggestions_needs_an_existing_raw_job(conn: sqlite3.Connection) -> None:
    db.migrate_up(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO job_field_suggestions (raw_job_id, field_name, value, reason)
            VALUES (99, 'recruitment_end_at', '2026-09-30', '다르다')
            """
        )


def test_job_field_suggestions_down_drops_only_its_own_table(conn: sqlite3.Connection) -> None:
    """역적용은 0023 이 만든 표만 지운다. 수집·정규화 데이터는 그대로다 (11.1.V)."""
    db.migrate_up(conn)
    _seed_raw_job(conn)
    conn.execute(
        """
        INSERT INTO job_field_suggestions (raw_job_id, field_name, value, reason)
        VALUES (1, 'recruitment_end_at', '2026-09-30', '원문과 다르다')
        """
    )

    db.migrate_down(conn, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0023"))

    assert "job_field_suggestions" not in _names(conn, "table")
    assert conn.execute("SELECT count(*) AS n FROM raw_jobs").fetchone()["n"] == 1


def test_job_field_suggestions_up_after_down_restores_the_table(conn: sqlite3.Connection) -> None:
    """적용·역적용·재적용이 같은 스키마로 돌아오는지 (11.1.V)."""
    db.migrate_up(conn)

    db.migrate_down(conn, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0023"))
    assert "job_field_suggestions" not in _names(conn, "table")

    db.migrate_up(conn)
    assert _columns(conn, "job_field_suggestions") == EXPECTED_COLUMNS["job_field_suggestions"]


def _at_0027(connection: sqlite3.Connection) -> None:
    """0028 직전 상태로 만든다. 오공고가 받는 다섯 칸이 아직 없는 스키마다."""
    db.migrate_up(connection)
    db.migrate_down(connection, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0028"))


POSTING_DETAIL_COLUMNS = (
    "company_and_team_introduction",
    "compensation",
    "benefits",
    "education_level",
    "recruitment_headcount",
)


def test_posting_detail_fields_are_added_to_both_tables(conn: sqlite3.Connection) -> None:
    """분류가 앉히는 자리와 소비 측이 읽는 자리 둘 다에 있어야 값이 끝까지 간다."""
    _at_0027(conn)
    for table in ("normalized_jobs", "job_classifications"):
        assert not set(POSTING_DETAIL_COLUMNS) & _columns(conn, table), table

    db.migrate_up(conn)

    for table in ("normalized_jobs", "job_classifications"):
        assert set(POSTING_DETAIL_COLUMNS) <= _columns(conn, table), table


def test_posting_detail_fields_leave_the_old_headcount_alone(conn: sqlite3.Connection) -> None:
    """모집인원은 새 이름이다. 0016 이전 분류기가 남긴 `headcount` 는 지우지 않는다."""
    db.migrate_up(conn)

    assert "headcount" in _columns(conn, "job_classifications")
    assert "headcount" not in _columns(conn, "normalized_jobs")


def test_the_posting_detail_down_drops_the_columns_and_their_corrections(
    conn: sqlite3.Connection,
) -> None:
    """되돌리면 다섯 칸의 값과 거기 걸린 보정·제안만 사라진다. 나머지는 그대로다."""
    db.migrate_up(conn)
    _seed_raw_job(conn)
    conn.executemany(
        "INSERT INTO job_field_overrides (raw_job_id, field_name, value) VALUES (1, ?, ?)",
        [("title", "사람이 고친 제목"), ("compensation", "연봉 협의")],
    )
    conn.executemany(
        """
        INSERT INTO job_field_suggestions (raw_job_id, field_name, value, reason)
        VALUES (1, ?, '제안 값', '원문과 다르다')
        """,
        [("title",), ("education_level",)],
    )

    db.migrate_down(conn, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0028"))

    for table in ("normalized_jobs", "job_classifications"):
        assert not set(POSTING_DETAIL_COLUMNS) & _columns(conn, table), table
    overrides = conn.execute("SELECT field_name FROM job_field_overrides").fetchall()
    assert [row["field_name"] for row in overrides] == ["title"]
    suggestions = conn.execute("SELECT field_name FROM job_field_suggestions").fetchall()
    assert [row["field_name"] for row in suggestions] == ["title"]


def _at_0028(connection: sqlite3.Connection) -> None:
    """0029 직전 상태로 만든다. 공고를 나눌 번호 칸이 아직 없는 스키마다."""
    db.migrate_up(connection)
    db.migrate_down(connection, steps=len(ALL_VERSIONS) - ALL_VERSIONS.index("0029"))


SPLIT_TABLES = (
    "normalized_jobs",
    "job_classifications",
    "job_field_overrides",
    "job_field_suggestions",
)


def _seed_postings(connection: sqlite3.Connection, parts: tuple[int, ...] | None) -> None:
    """네 표에 행을 하나씩. `parts` 가 None 이면 번호 칸이 없던 때처럼 번호를 적지 않는다."""
    _seed_raw_job(connection)
    # 0031 이 `duties` 를 `responsibilities` 로 바꿨다. 어느 쪽 스키마에서 부르든 같은 칸에 넣는다
    duties = (
        "duties" if "duties" in _columns(connection, "job_classifications") else "responsibilities"
    )
    for part in parts or (None,):
        number = "" if part is None else ", part"
        value = "" if part is None else f", {part}"
        connection.execute(
            f"INSERT INTO job_classifications (raw_job_id, model, {duties}{number})"
            f" VALUES (1, 'model', '결제 서버 개발'{value})"
        )
        connection.execute(
            f"INSERT INTO normalized_jobs (raw_job_id, source_url, title{number})"
            f" VALUES (1, 'https://example.test/1', '백엔드'{value})"
        )
        connection.execute(
            f"INSERT INTO job_field_overrides (raw_job_id, field_name, value{number})"
            f" VALUES (1, 'title', '사람이 고친 제목'{value})"
        )
        connection.execute(
            f"INSERT INTO job_field_suggestions (raw_job_id, field_name, value{number})"
            f" VALUES (1, 'title', '제안 값'{value})"
        )


def test_existing_rows_become_the_first_posting(conn: sqlite3.Connection) -> None:
    """나누지 않은 공고는 1번 하나다. 값은 그대로 옮겨진다."""
    _at_0028(conn)
    _seed_postings(conn, None)

    _up_to_0030(conn)

    for table in SPLIT_TABLES:
        rows = conn.execute(f"SELECT part FROM {table}").fetchall()
        assert [row["part"] for row in rows] == [1], table
    row = conn.execute("SELECT duties, part_role, part_lines FROM job_classifications").fetchone()
    assert (row["duties"], row["part_role"], row["part_lines"]) == ("결제 서버 개발", None, None)


def test_one_raw_job_holds_several_postings_but_each_number_once(
    conn: sqlite3.Connection,
) -> None:
    """삼성 공고 하나가 열두 공고가 된다. 같은 번호의 같은 칸은 여전히 하나다."""
    db.migrate_up(conn)
    _seed_postings(conn, (1, 2))

    for table in SPLIT_TABLES:
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 2, table
    for sql in (
        "INSERT INTO job_classifications (raw_job_id, part, model) VALUES (1, 2, 'model')",
        "INSERT INTO job_field_overrides (raw_job_id, part, field_name, value)"
        " VALUES (1, 2, 'title', '또 고친 제목')",
        "INSERT INTO job_field_suggestions (raw_job_id, part, field_name, value)"
        " VALUES (1, 2, 'title', '또 다른 제안')",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO normalized_jobs (raw_job_id, source_url, part)"
        " VALUES (1, 'https://example.test/1', 0)",
        "INSERT INTO job_classifications (raw_job_id, model, part) VALUES (1, 'model', 0)",
        "INSERT INTO job_field_overrides (raw_job_id, field_name, value, part)"
        " VALUES (1, 'title', '값', 0)",
        "INSERT INTO job_field_suggestions (raw_job_id, field_name, value, part)"
        " VALUES (1, 'title', '값', 0)",
    ],
)
def test_a_posting_number_starts_at_one(conn: sqlite3.Connection, sql: str) -> None:
    db.migrate_up(conn)
    _seed_raw_job(conn)

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql)


def test_the_split_down_keeps_only_the_first_posting(conn: sqlite3.Connection) -> None:
    """되돌리면 2번 이후 행이 네 표 모두에서 떨어지고 번호 칸이 사라진다."""
    db.migrate_up(conn)
    _seed_postings(conn, (1, 2))

    # 0030·0031 이 뒤에 붙었다. 몇 걸음인지 세지 않고 0029 앞까지 되돌린다
    _down_to_before(conn, "0029")

    for table in SPLIT_TABLES:
        assert "part" not in _columns(conn, table), table
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1, table
    assert not {"part_role", "part_lines"} & _columns(conn, "job_classifications")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO job_classifications (raw_job_id, model) VALUES (1, 'model')")
