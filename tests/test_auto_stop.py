"""실패 임계치와 자동 중지 테스트.

실사이트에 나가지 않는다. 성공과 실패를 골라 낼 수 있게 fetch 클라이언트를 스텁으로 두고,
목록 셀렉터를 갈아 끼워 실패한 실행을 만든다.

확인하는 것은 두 가지다. 실행 결과가 `workflows` 의 누적값에 반영되는가, 그리고 연속 실패가
임계치에 닿았을 때만 `paused` 가 되는가.
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
from app.scheduler import WorkflowScheduler, job_id

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
LIST_HTML = (FIXTURES / "pythonorg-jobs-list-20260821.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "pythonorg-job-detail-20260821.html").read_text(encoding="utf-8")

LIST_URL = "https://www.python.org/jobs/"
ROBOTS = "User-agent: *\nDisallow:\n"

WORKING: dict[str, Any] = {
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


def broken() -> dict[str, Any]:
    """목록이 하나도 매칭되지 않는 셀렉터. 실행은 `selector_miss` 로 실패한다."""
    copy = json.loads(json.dumps(WORKING))
    copy["list"]["item"] = "ol.list-of-nothing > li"
    return copy


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
        ("python.org", LIST_URL, json.dumps(WORKING)),
    )
    try:
        yield connection
    finally:
        connection.close()


def add_workflow(conn: sqlite3.Connection, threshold: int | None) -> int:
    cursor = conn.execute(
        """
        INSERT INTO workflows (crawler_id, name, interval_minutes, auto_stop_threshold)
        VALUES (1, ?, 60, ?)
        """,
        ("python.org 채용", threshold),
    )
    return int(cursor.lastrowid or 0)


def use(conn: sqlite3.Connection, selectors: dict[str, Any]) -> None:
    conn.execute("UPDATE crawlers SET selectors_json = ? WHERE id = 1", (json.dumps(selectors),))


def workflow(conn: sqlite3.Connection, workflow_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()


async def run(conn: sqlite3.Connection, workflow_id: int) -> str:
    result = await run_workflow(conn, workflow_id, fetcher=stub_fetcher(), limit=1)
    return result.status


async def test_실행_결과가_누적값과_마지막_실행_시각에_반영된다(
    conn: sqlite3.Connection,
) -> None:
    workflow_id = add_workflow(conn, threshold=None)
    assert workflow(conn, workflow_id)["last_run_at"] is None

    assert await run(conn, workflow_id) == "success"
    use(conn, broken())
    assert await run(conn, workflow_id) == "failed"

    row = workflow(conn, workflow_id)
    assert (row["success_count"], row["fail_count"]) == (1, 1)
    assert row["last_run_at"] is not None


async def test_임계치_3_에서_연속_3회_실패하면_paused_가_된다(
    conn: sqlite3.Connection,
) -> None:
    workflow_id = add_workflow(conn, threshold=3)
    use(conn, broken())

    for _ in range(2):
        assert await run(conn, workflow_id) == "failed"
    # 아직 임계치에 닿지 않았다
    assert workflow(conn, workflow_id)["status"] == "active"

    assert await run(conn, workflow_id) == "failed"

    row = workflow(conn, workflow_id)
    assert row["status"] == "paused"
    assert row["fail_count"] == 3


async def test_중간에_성공이_끼면_연속이_끊겨_유지된다(conn: sqlite3.Connection) -> None:
    workflow_id = add_workflow(conn, threshold=3)

    use(conn, broken())
    await run(conn, workflow_id)
    await run(conn, workflow_id)
    use(conn, WORKING)
    assert await run(conn, workflow_id) == "success"
    use(conn, broken())
    await run(conn, workflow_id)
    await run(conn, workflow_id)

    row = workflow(conn, workflow_id)
    assert row["status"] == "active"
    assert (row["success_count"], row["fail_count"]) == (1, 4)


async def test_임계치가_NULL_이면_자동_중지하지_않는다(conn: sqlite3.Connection) -> None:
    workflow_id = add_workflow(conn, threshold=None)
    use(conn, broken())

    for _ in range(5):
        await run(conn, workflow_id)

    row = workflow(conn, workflow_id)
    assert row["status"] == "active"
    assert row["fail_count"] == 5


async def test_실행하지_못한_실패도_연속으로_센다(conn: sqlite3.Connection) -> None:
    """셀렉터를 읽지 못해 크롤링에 들어가지도 못한 실행이다. 그것도 실패한 실행이다."""
    workflow_id = add_workflow(conn, threshold=2)
    conn.execute("UPDATE crawlers SET selectors_json = NULL WHERE id = 1")

    for _ in range(2):
        assert await run(conn, workflow_id) == "failed"

    assert workflow(conn, workflow_id)["status"] == "paused"


async def test_자동_중지된_워크플로우는_잡에서도_빠진다(conn: sqlite3.Connection) -> None:
    """테이블만 바뀌고 잡이 남으면 멈춘 워크플로우가 계속 깨어난다."""
    workflow_id = add_workflow(conn, threshold=1)
    scheduler = WorkflowScheduler()
    scheduler.sync(conn)
    assert scheduler.scheduled() == {workflow_id: 60}

    use(conn, broken())
    await run(conn, workflow_id)
    scheduler.sync(conn)

    assert scheduler.scheduled() == {}
    assert scheduler.scheduler.get_job(job_id(workflow_id)) is None
