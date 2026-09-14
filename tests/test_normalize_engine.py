"""규칙 엔진 테스트.

원문 값이 들어가서 어떤 값이 나오는지를 타입별로 단언하고, 같은 필드에 규칙이 여럿일 때의
적용 순서를 확인한다.

원문은 저장된 python.org 픽스처에서 뽑는다. 실사이트에 나가지 않는다.

마지막 테스트가 이 파일의 이유다. 정규화를 돌린 뒤 `raw_jobs` 가 한 바이트도 달라지지
않았는지 본다 (`.claude/rules/data-safety.md`).
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from app import db
from app.crawler.parser import parse_detail
from app.normalize.engine import (
    NormalizeError,
    RawJobMissingError,
    insert_normalized,
    load_rules,
    normalize_fields,
)
from app.normalize.rules import NORMALIZED_FIELDS, build_rule
from app.selector.schema import DetailSelectors

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
DETAIL_HTML = (FIXTURES / "pythonorg-job-detail-20260821.html").read_text(encoding="utf-8")

DETAIL_SELECTORS = DetailSelectors(
    title="h1.listing-company span.company-name",
    body="div.job-description",
    qualifications="",
    recruitment_end_at="",
    department="span.listing-company-category a",
)


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    try:
        yield connection
    finally:
        connection.close()


def fixture_record() -> dict[str, str]:
    """픽스처에서 뽑은 원문 필드. `raw_jobs.raw_data_json` 에 들어가는 모양 그대로다."""
    parsed = parse_detail(DETAIL_HTML, DETAIL_SELECTORS)
    return {"source_url": "https://www.python.org/jobs/7891/", **parsed.fields}


def add_raw(conn: sqlite3.Connection, record: dict[str, Any]) -> int:
    conn.execute(
        "INSERT INTO crawlers (name, list_url) VALUES ('python.org', 'https://www.python.org/jobs/')"
    )
    conn.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, 'python.org')")
    cursor = conn.execute(
        """
        INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash)
        VALUES (1, ?, ?, 'hash-1')
        """,
        (record["source_url"], json.dumps(record, ensure_ascii=False)),
    )
    return int(cursor.lastrowid or 0)


def raw_snapshot(conn: sqlite3.Connection) -> str:
    """`raw_jobs` 전체를 바이트로 굳힌 값. 한 글자만 달라져도 해시가 바뀐다."""
    rows = conn.execute(
        """
        SELECT id, workflow_id, source_url, raw_data_json, content_hash, crawled_at
          FROM raw_jobs ORDER BY id
        """
    ).fetchall()
    digest = hashlib.sha256()
    for row in rows:
        for value in tuple(row):
            digest.update(repr(value).encode("utf-8"))
            digest.update(b"\x1f")
        digest.update(b"\x1e")
    return digest.hexdigest()


def test_trim_collapses_whitespace() -> None:
    """값이 있는 필드만 채워진다. 마감일도 없으면 비어 있다 — 상시 채용이다 (2026-09-14 결정)."""
    rule = build_rule("title", "trim", {})

    fields = normalize_fields({"title": "  파이썬  \n  백엔드 개발자 "}, [rule])

    assert set(fields) == {*NORMALIZED_FIELDS, "parent_company_name"}
    assert fields["title"] == "파이썬 백엔드 개발자"
    assert fields["recruitment_end_at"] is None
    assert [name for name, value in fields.items() if value is not None] == ["title"]


def test_trim_with_strip_chars() -> None:
    rule = build_rule("region", "trim", {"collapse_whitespace": False, "strip_chars": "-· "})
    assert normalize_fields({"region": "-· 판교 ·-"}, [rule])["region"] == "판교"


def test_regex_removes_matched_text() -> None:
    rule = build_rule("title", "regex", {"pattern": r"\[광고\]\s*", "replacement": ""})
    assert normalize_fields({"title": "[광고] 백엔드 개발자"}, [rule])["title"] == "백엔드 개발자"


def test_mapping_replaces_exact_value() -> None:
    rule = build_rule("region", "mapping", {"map": {"Pangyo": "판교"}})
    assert normalize_fields({"region": "Pangyo"}, [rule])["region"] == "판교"


def test_mapping_keeps_value_without_default() -> None:
    rule = build_rule("region", "mapping", {"map": {"Pangyo": "판교"}})
    assert normalize_fields({"region": "Seoul"}, [rule])["region"] == "Seoul"


def test_mapping_uses_default_when_missing() -> None:
    rule = build_rule("region", "mapping", {"map": {"Pangyo": "판교"}, "default": "기타"})
    assert normalize_fields({"region": "Seoul"}, [rule])["region"] == "기타"


def test_date_parse_reformats() -> None:
    rule = build_rule(
        "recruitment_end_at", "date_parse", {"formats": ["%Y년 %m월 %d일", "%Y.%m.%d"]}
    )
    # 시각 없이 날짜만 읽은 마감일은 그날이 끝날 때까지다 (2026-09-14 결정)
    assert (
        normalize_fields({"recruitment_end_at": "2026.09.30"}, [rule])["recruitment_end_at"]
        == "2026-09-30 23:59:59"
    )
    assert (
        normalize_fields({"recruitment_end_at": "2026년 9월 3일"}, [rule])["recruitment_end_at"]
        == "2026-09-03 23:59:59"
    )


def test_date_parse_keeps_a_written_time_and_starts_a_bare_start_date_at_midnight() -> None:
    """적힌 시각은 그대로 쓴다. 날짜만 있는 시작일은 그날 00:00:00 이다."""
    formats = {"formats": ["%Y.%m.%d %H:%M", "%Y.%m.%d"]}
    end = build_rule("recruitment_end_at", "date_parse", formats)
    start = build_rule("recruitment_start_at", "date_parse", formats)

    fields = normalize_fields(
        {"recruitment_end_at": "2026.09.30 18:00", "recruitment_start_at": "2026.09.01"},
        [end, start],
    )

    assert fields["recruitment_end_at"] == "2026-09-30 18:00:00"
    assert fields["recruitment_start_at"] == "2026-09-01 00:00:00"


def test_date_parse_failure_is_an_error() -> None:
    """읽지 못한 값을 원문 그대로 통과시키지 않는다. 마감 칸이 날짜가 아니게 된다."""
    rule = build_rule("recruitment_end_at", "date_parse", {"formats": ["%Y.%m.%d"]})
    with pytest.raises(NormalizeError) as caught:
        normalize_fields({"recruitment_end_at": "상시채용"}, [rule])
    assert caught.value.field_name == "recruitment_end_at"
    assert caught.value.rule_type == "date_parse"


def test_priority_decides_order() -> None:
    """앞 규칙의 결과가 뒤 규칙의 입력이다. 순서가 뒤집히면 결과가 달라진다."""
    strip_prefix = build_rule(
        "recruitment_end_at", "regex", {"pattern": "^마감\\s*:\\s*", "replacement": ""}, priority=0
    )
    parse = build_rule("recruitment_end_at", "date_parse", {"formats": ["%Y.%m.%d"]}, priority=1)

    assert (
        normalize_fields({"recruitment_end_at": "마감: 2026.09.30"}, [parse, strip_prefix])[
            "recruitment_end_at"
        ]
        == "2026-09-30 23:59:59"
    )

    # 순서를 뒤집으면 날짜로 읽을 수 없다. 우선순위가 실제로 적용된다는 증거다
    flipped = build_rule("recruitment_end_at", "date_parse", {"formats": ["%Y.%m.%d"]}, priority=-1)
    with pytest.raises(NormalizeError):
        normalize_fields({"recruitment_end_at": "마감: 2026.09.30"}, [strip_prefix, flipped])


def test_same_priority_falls_back_to_id() -> None:
    first = build_rule("title", "regex", {"pattern": "A", "replacement": "B"}, rule_id=1)
    second = build_rule("title", "regex", {"pattern": "B", "replacement": "C"}, rule_id=2)
    assert normalize_fields({"title": "A"}, [second, first])["title"] == "C"


def test_disabled_rule_is_skipped() -> None:
    off = build_rule(
        "title", "regex", {"pattern": "개발자", "replacement": "엔지니어"}, enabled=False
    )
    on = build_rule("title", "trim", {}, priority=1)
    assert normalize_fields({"title": " 백엔드 개발자 "}, [off, on])["title"] == "백엔드 개발자"


def test_empty_value_skips_rules() -> None:
    """값이 없는 필드에 규칙을 태우지 않는다. 없는 값이 규칙 실패로 둔갑하지 않는다."""
    rule = build_rule("region", "date_parse", {"formats": ["%Y.%m.%d"]})
    assert normalize_fields({"region": ""}, [rule])["region"] is None
    assert normalize_fields({}, [rule])["region"] is None
    assert normalize_fields({}, [rule])["recruitment_end_at"] is None


def test_no_rules_passes_values_through() -> None:
    record = fixture_record()
    fields = normalize_fields(record, [])
    assert fields["title"] == record["title"]
    assert fields["body"] == record["body"]
    # 픽스처의 셀렉터가 뽑지 않는 필드는 NULL 이다
    assert fields["company_name"] is None
    assert fields["recruitment_end_at"] is None


def test_마감을_못_뽑으면_마감일은_비어_있다() -> None:
    """2026-09-14 결정. "상시모집" 글자로 채우지 않는다 — 오공고는 마감 일시 칸에 일시만 받는다.

    상시 채용인지는 정규화의 마무리가 모집 유형으로 가른다 (`tests/test_normalize_settle.py`).
    """
    record = fixture_record()
    assert "recruitment_end_at" not in record or not record.get("recruitment_end_at")

    fields = normalize_fields(record, [])

    assert fields["recruitment_end_at"] is None


def test_마감이_있으면_그_일시가_남는다() -> None:
    record = {**fixture_record(), "recruitment_end_at": "2026.09.30"}
    rule = build_rule("recruitment_end_at", "date_parse", {"formats": ["%Y.%m.%d"]})

    fields = normalize_fields(record, [rule])

    assert fields["recruitment_end_at"] == "2026-09-30 23:59:59"


def test_규칙이_마감을_비우면_마감일은_비어_있다() -> None:
    """`상시채용` 을 빈 값으로 매핑하는 규칙(운영 규칙)과 같은 경로다."""
    record = {**fixture_record(), "recruitment_end_at": "상시채용"}
    rule = build_rule("recruitment_end_at", "mapping", {"map": {"상시채용": ""}})

    fields = normalize_fields(record, [rule])

    assert fields["recruitment_end_at"] is None


def test_판단_못한_경력_구분은_경력_무관으로_채워진다() -> None:
    """2026-08-28 결정. 분류가 `experience_type` 을 비웠으면 `IRRELEVANT`(경력 무관)를 넣는다.

    사이트가 경력을 아예 언급하지 않은 공고 대부분이 실제로 경력무관이라, 근거 없어 판단
    못한 것과 결과적으로 같은 값이 되는 편이 검수 화면에서 유용하다. 다른 여덟 칸은 이
    규칙을 타지 않는다 — 빈 칸이 그대로 있어야 못 뽑은 것을 구분할 수 있다.
    """
    record = fixture_record()
    fields = normalize_fields(record, [], classification={"experience_type": ""})
    assert fields["experience_type"] == "IRRELEVANT"
    assert fields["employment_type"] is None


def test_경력_구분이_있으면_경력_무관으로_덮지_않는다() -> None:
    record = fixture_record()
    fields = normalize_fields(record, [], classification={"experience_type": "EXPERIENCED"})
    assert fields["experience_type"] == "EXPERIENCED"


def test_분류가_없으면_경력_구분도_채우지_않는다() -> None:
    """분류가 아직 안 돈 건은 규칙이 만든 값(대개 비어 있음) 그대로다."""
    record = fixture_record()
    fields = normalize_fields(record, [], classification=None)
    assert fields["experience_type"] is None


def test_직무_대분류_소분류가_분류_결과로_채워진다() -> None:
    """`job_field`/`job_role` 도 다른 아홉 칸과 같은 경로(`CLASSIFY_FIELDS`)를 탄다."""
    record = fixture_record()
    fields = normalize_fields(
        record, [], classification={"job_field": "IT·개발", "job_role": "서버·백엔드"}
    )
    assert fields["job_field"] == "IT·개발"
    assert fields["job_role"] == "서버·백엔드"


def test_대분류만_있고_소분류가_비면_소분류는_None이다() -> None:
    """본문으로 소분류가 갈리지 않는 공고. 찍어서 채우지 않는다(PRD 2절)."""
    record = fixture_record()
    fields = normalize_fields(record, [], classification={"job_field": "IT·개발"})
    assert fields["job_field"] == "IT·개발"
    assert fields["job_role"] is None


def test_분류가_없으면_직무_분류도_비어있다() -> None:
    record = fixture_record()
    fields = normalize_fields(record, [], classification=None)
    assert fields["job_field"] is None
    assert fields["job_role"] is None


def test_load_rules_reads_stored_rows(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO normalization_rules (field_name, rule_type, rule_config_json, priority, enabled)
        VALUES ('title', 'trim', '{}', 2, 1), ('title', 'regex', '{"pattern": "x"}', 1, 0)
        """
    )
    rules = load_rules(conn)
    assert [(rule.field_name, rule.rule_type, rule.priority, rule.enabled) for rule in rules] == [
        ("title", "regex", 1, False),
        ("title", "trim", 2, True),
    ]


def test_load_rules_rejects_broken_stored_config(conn: sqlite3.Connection) -> None:
    """손으로 고쳐 넣은 깨진 설정을 조용히 넘기지 않는다."""
    conn.execute(
        """
        INSERT INTO normalization_rules (field_name, rule_type, rule_config_json)
        VALUES ('title', 'regex', '{"pattern": "(["}')
        """
    )
    with pytest.raises(NormalizeError):
        load_rules(conn)


def test_insert_normalized_writes_one_row(conn: sqlite3.Connection) -> None:
    record = fixture_record()
    raw_id = add_raw(conn, record)
    rule = build_rule("title", "trim", {})

    normalized_id = insert_normalized(conn, raw_id, [rule])

    row = conn.execute("SELECT * FROM normalized_jobs WHERE id = ?", (normalized_id,)).fetchone()
    assert row["raw_job_id"] == raw_id
    assert row["source_url"] == record["source_url"]
    # trim 은 양끝을 깎고 연속 공백을 하나로 접는다
    assert row["title"] == " ".join(record["title"].split())
    assert row["normalized_at"]
    # 제공 API 경로만 쓴다. 정규화는 건드리지 않는다
    assert row["delivered_at"] is None


def test_missing_raw_job_is_reported(conn: sqlite3.Connection) -> None:
    with pytest.raises(RawJobMissingError):
        insert_normalized(conn, 999, [])


def test_broken_raw_data_json_fails_that_row(conn: sqlite3.Connection) -> None:
    add_raw(conn, fixture_record())
    conn.execute("UPDATE raw_jobs SET raw_data_json = '{not json' WHERE id = 1")
    with pytest.raises(NormalizeError):
        insert_normalized(conn, 1, [])


def test_raw_jobs_is_untouched_by_normalization(conn: sqlite3.Connection) -> None:
    """정규화 전후로 `raw_jobs` 가 바이트 단위로 같아야 한다.

    성공 경로와 실패 경로를 모두 태운다. 실패한 규칙이 raw 를 되돌리거나 지우지 않는지까지
    보기 위해서다.
    """
    record = fixture_record()
    raw_id = add_raw(conn, record)
    before = raw_snapshot(conn)
    before_json = conn.execute(
        "SELECT raw_data_json FROM raw_jobs WHERE id = ?", (raw_id,)
    ).fetchone()["raw_data_json"]

    insert_normalized(conn, raw_id, [build_rule("title", "trim", {})])
    insert_normalized(
        conn, raw_id, [build_rule("body", "regex", {"pattern": "\\s+", "replacement": " "})]
    )
    with pytest.raises(NormalizeError):
        insert_normalized(
            conn, raw_id, [build_rule("title", "date_parse", {"formats": ["%Y.%m.%d"]})]
        )

    assert raw_snapshot(conn) == before
    after_json = conn.execute(
        "SELECT raw_data_json FROM raw_jobs WHERE id = ?", (raw_id,)
    ).fetchone()["raw_data_json"]
    assert after_json == before_json
    assert after_json.encode("utf-8") == json.dumps(record, ensure_ascii=False).encode("utf-8")
    assert conn.execute("SELECT count(*) AS n FROM raw_jobs").fetchone()["n"] == 1


def test_rule_that_empties_a_value_stops_the_chain() -> None:
    """규칙이 값을 비우면 뒤 규칙에 넘기지 않는다.

    "상시채용" 을 mapping 으로 비운 뒤 date_parse 가 그 빈 값을 읽으려 하면 실패가 나고,
    그 공고가 통째로 `normalized_jobs` 에서 빠진다. 멈추면 마감일만 비고 공고는 남는다.
    """
    rules = [
        build_rule(
            field_name="recruitment_end_at",
            rule_type="mapping",
            config={"map": {"상시채용": ""}},
            priority=0,
            rule_id=1,
        ),
        build_rule(
            field_name="recruitment_end_at",
            rule_type="date_parse",
            config={"formats": ["%Y-%m-%d"]},
            priority=10,
            rule_id=2,
        ),
    ]

    out = normalize_fields({"title": "개발자", "recruitment_end_at": "상시채용"}, rules)

    assert out["recruitment_end_at"] is None
    assert out["title"] == "개발자"


def test_a_real_date_still_goes_through_the_whole_chain() -> None:
    """빈 값에서만 멈춘다. 값이 남아 있으면 뒤 규칙이 전부 돈다."""
    rules = [
        build_rule(
            field_name="recruitment_end_at",
            rule_type="regex",
            config={"pattern": "^.*?[~〜]\\s*", "replacement": ""},
            priority=0,
            rule_id=1,
        ),
        build_rule(
            field_name="recruitment_end_at",
            rule_type="regex",
            config={"pattern": "\\s*\\d{1,2}:\\d{2}\\s*$", "replacement": ""},
            priority=10,
            rule_id=2,
        ),
        build_rule(
            field_name="recruitment_end_at",
            rule_type="date_parse",
            config={"formats": ["%Y-%m-%d"]},
            priority=20,
            rule_id=3,
        ),
    ]

    out = normalize_fields({"recruitment_end_at": "2026-08-15 09:00 ~ 2026-08-30 17:00"}, rules)

    # 시각을 떼는 규칙을 거쳐 날짜만 남았으니 그날이 끝날 때까지다
    assert out["recruitment_end_at"] == "2026-08-30 23:59:59"
