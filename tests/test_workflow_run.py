"""워크플로우 실행 진입점 테스트.

실사이트에 나가지 않는다. 저장된 python.org 픽스처를 돌려주는 스텁 fetch 클라이언트를 쓴다.

확인하는 것은 두 가지다. 실행 대상을 잡이 아니라 테이블에서 읽는가, 그리고 실행하지 못한
경우에도 `crawl_runs` 행이 남는가.
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
from app.crawler.runner import WorkflowMissingError, run_workflow

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
    try:
        yield connection
    finally:
        connection.close()


def add_workflow(conn: sqlite3.Connection, selectors: Any) -> int:
    conn.execute(
        """
        INSERT INTO crawlers (name, list_url, selectors_json, status)
        VALUES (?, ?, ?, 'promoted')
        """,
        ("python.org", LIST_URL, selectors if selectors is None else json.dumps(selectors)),
    )
    cursor = conn.execute(
        "INSERT INTO workflows (crawler_id, name, interval_minutes) VALUES (1, ?, 60)",
        ("python.org 채용",),
    )
    return int(cursor.lastrowid or 0)


def runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM crawl_runs").fetchall()


async def test_실행_대상을_테이블에서_읽어_적재한다(conn: sqlite3.Connection) -> None:
    workflow_id = add_workflow(conn, SELECTORS)

    result = await run_workflow(conn, workflow_id, fetcher=stub_fetcher(), limit=2)

    assert result.status == "success"
    assert (result.success_count, result.new_count) == (2, 2)

    stored = runs(conn)
    assert len(stored) == 1
    assert stored[0]["workflow_id"] == workflow_id
    assert stored[0]["crawler_id"] is None
    # 테스트 실행과 달리 워크플로우 실행은 raw_jobs 에 적재한다
    assert conn.execute("SELECT count(*) AS n FROM raw_jobs").fetchone()["n"] == 2


async def test_두_번째_실행은_아는_공고를_다시_적재하지_않는다(
    conn: sqlite3.Connection,
) -> None:
    workflow_id = add_workflow(conn, SELECTORS)
    fetcher = stub_fetcher()
    await run_workflow(conn, workflow_id, fetcher=fetcher, limit=2)

    second = await run_workflow(conn, workflow_id, fetcher=fetcher, limit=2)

    assert second.status == "success"
    assert second.new_count == 0
    assert len(runs(conn)) == 2
    assert conn.execute("SELECT count(*) AS n FROM raw_jobs").fetchone()["n"] == 2


async def test_셀렉터가_없어도_crawl_runs_행은_남는다(conn: sqlite3.Connection) -> None:
    workflow_id = add_workflow(conn, None)

    result = await run_workflow(conn, workflow_id, fetcher=stub_fetcher())

    assert result.status == "failed"
    # transport·selector_miss·parse 중 어느 것도 아니다. 모르는 채로 두고 사유만 남긴다
    assert result.error_class is None
    assert "셀렉터" in result.error_message

    stored = runs(conn)
    assert len(stored) == 1
    assert stored[0]["workflow_id"] == workflow_id
    assert stored[0]["status"] == "failed"
    assert stored[0]["finished_at"] is not None


async def test_셀렉터가_스키마에_맞지_않아도_행은_남는다(conn: sqlite3.Connection) -> None:
    workflow_id = add_workflow(conn, {"list": {"item": "li"}})

    result = await run_workflow(conn, workflow_id, fetcher=stub_fetcher())

    assert result.status == "failed"
    assert len(runs(conn)) == 1


async def test_셀렉터_미스도_실패로_남는다(conn: sqlite3.Connection) -> None:
    broken = json.loads(json.dumps(SELECTORS))
    broken["list"]["item"] = "ol.list-of-nothing > li"
    workflow_id = add_workflow(conn, broken)

    result = await run_workflow(conn, workflow_id, fetcher=stub_fetcher())

    assert result.status == "failed"
    assert result.error_class == "selector_miss"
    assert runs(conn)[0]["status"] == "failed"


async def test_없는_워크플로우는_기록할_곳이_없어_예외다(conn: sqlite3.Connection) -> None:
    with pytest.raises(WorkflowMissingError):
        await run_workflow(conn, 999, fetcher=stub_fetcher())

    assert runs(conn) == []
