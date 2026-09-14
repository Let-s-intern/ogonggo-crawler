"""실행 파이프라인의 정규화 단계 테스트.

저장된 python.org 픽스처를 돌려주는 스텁 fetch 클라이언트로 워크플로우를 1회 돌린다.
실사이트에 나가지 않는다.

확인하는 것은 둘이다. 적재한 건마다 `normalized_jobs` 행이 하나 생기는가, 그리고 규칙이
예외를 던져도 `raw_jobs` 는 남고 실행이 계속되는가.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from app import db
from app.config import Settings
from app.crawler.fetcher import Fetcher
from app.crawler.runner import run_workflow

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
LIST_HTML = (FIXTURES / "pythonorg-jobs-list-20260821.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "pythonorg-job-detail-20260821.html").read_text(encoding="utf-8")

LIST_URL = "https://www.python.org/jobs/"
ROBOTS = "User-agent: *\nDisallow:\n"

SELECTORS: dict[str, Any] = {
    "list": {
        "item": "ol.list-recent-jobs > li",
        "title": "span.listing-company-name > a",
        "link": "span.listing-company-name > a",
        "date": "span.listing-posted time",
    },
    "detail": {
        "title": "h1.listing-company span.company-name",
        "body": "div.job-description",
        "qualifications": "",
        "recruitment_end_at": "",
        "department": "span.listing-company-category a",
    },
}


def stub_fetcher() -> Fetcher:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        if request.url.path == "/jobs/":
            return httpx.Response(200, text=LIST_HTML)
        return httpx.Response(200, text=DETAIL_HTML)

    async def no_wait(seconds: float) -> None:
        return None

    return Fetcher(
        settings=Settings(crawl_delay_seconds=0.0, crawl_max_retries=0),
        transport=httpx.MockTransport(handle),
        sleep=no_wait,
    )


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        """
        INSERT INTO crawlers (name, list_url, selectors_json, status)
        VALUES (?, ?, ?, 'promoted')
        """,
        ("python.org", LIST_URL, json.dumps(SELECTORS)),
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, 'python.org')")
    try:
        yield connection
    finally:
        connection.close()


def add_rule(
    conn: sqlite3.Connection,
    field_name: str,
    rule_type: str,
    config: dict[str, Any] | str,
    priority: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO normalization_rules (field_name, rule_type, rule_config_json, priority)
        VALUES (?, ?, ?, ?)
        """,
        (
            field_name,
            rule_type,
            config if isinstance(config, str) else json.dumps(config),
            priority,
        ),
    )


def counts(conn: sqlite3.Connection) -> tuple[int, int]:
    raw = conn.execute("SELECT count(*) AS n FROM raw_jobs").fetchone()["n"]
    normalized = conn.execute("SELECT count(*) AS n FROM normalized_jobs").fetchone()["n"]
    return int(raw), int(normalized)


async def test_stored_row_gets_one_normalized_row(conn: sqlite3.Connection) -> None:
    result = await run_workflow(conn, 1, fetcher=stub_fetcher(), limit=2)

    raw, normalized = counts(conn)
    assert result.new_count == raw
    assert raw == 2
    assert normalized == raw

    rows = conn.execute("SELECT raw_job_id, source_url FROM normalized_jobs ORDER BY id").fetchall()
    raw_rows = conn.execute("SELECT id, source_url FROM raw_jobs ORDER BY id").fetchall()
    assert [row["raw_job_id"] for row in rows] == [row["id"] for row in raw_rows]
    assert [row["source_url"] for row in rows] == [row["source_url"] for row in raw_rows]


async def test_rules_are_applied_on_the_way_in(conn: sqlite3.Connection) -> None:
    add_rule(conn, "title", "trim", {})
    add_rule(conn, "title", "regex", {"pattern": r"\s*New\s*", "replacement": " "}, priority=1)

    await run_workflow(conn, 1, fetcher=stub_fetcher(), limit=1)

    raw_title = json.loads(
        conn.execute("SELECT raw_data_json FROM raw_jobs WHERE id = 1").fetchone()["raw_data_json"]
    )["title"]
    normalized_title = conn.execute(
        "SELECT title FROM normalized_jobs WHERE raw_job_id = 1"
    ).fetchone()["title"]

    assert "\n" in raw_title, "원문에는 개행이 있어야 이 테스트가 의미가 있다"
    assert normalized_title == "Software Engineer (Remote) Softech Associate"


async def test_failing_rule_keeps_raw_and_run_alive(conn: sqlite3.Connection) -> None:
    """날짜로 읽을 수 없는 값에 date_parse 를 걸어 규칙이 예외를 던지게 만든다."""
    add_rule(conn, "title", "date_parse", {"formats": ["%Y.%m.%d"]})

    result = await run_workflow(conn, 1, fetcher=stub_fetcher(), limit=2)

    raw, normalized = counts(conn)
    assert raw == 2, "정규화가 실패해도 수집한 원문은 남는다"
    assert normalized == 0
    assert result.new_count == 2
    assert result.fail_count == 2
    # 분류는 셋 중 어느 것도 아니다. 사유만 남는다
    assert all(failure.error_class is None for failure in result.failures)
    assert all("정규화 실패" in failure.message for failure in result.failures)


async def test_broken_stored_rule_does_not_stop_collection(conn: sqlite3.Connection) -> None:
    """DB 에 직접 넣은 깨진 설정. 정규화는 못 하지만 수집은 계속한다."""
    add_rule(conn, "title", "regex", json.dumps({"pattern": "(["}))

    result = await run_workflow(conn, 1, fetcher=stub_fetcher(), limit=1)

    raw, normalized = counts(conn)
    assert (raw, normalized) == (1, 0)
    assert result.fail_count == 1


async def test_run_row_is_written_either_way(conn: sqlite3.Connection) -> None:
    add_rule(conn, "title", "date_parse", {"formats": ["%Y.%m.%d"]})
    result = await run_workflow(conn, 1, fetcher=stub_fetcher(), limit=1)

    row = conn.execute("SELECT * FROM crawl_runs WHERE id = ?", (result.run_id,)).fetchone()
    assert row["finished_at"]
    assert row["new_count"] == 1
    assert row["fail_count"] == 1


async def test_second_run_normalizes_nothing_new(conn: sqlite3.Connection) -> None:
    """같은 공고를 다시 크롤링해도 raw 도 normalized 도 늘지 않는다."""
    await run_workflow(conn, 1, fetcher=stub_fetcher(), limit=2)
    before = counts(conn)
    await run_workflow(conn, 1, fetcher=stub_fetcher(), limit=2)
    assert counts(conn) == before
