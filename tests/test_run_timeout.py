"""실행 타임아웃 테스트.

실사이트에 나가지 않는다. 응답을 일부러 늦추는 스텁 fetch 클라이언트로 시간 제한을 넘긴다.

확인하는 것은 하나다. 실행이 제한을 넘겨 끊겨도 `crawl_runs` 행이 `status=timeout` 으로
남는가. 행이 없는 실행은 아무도 디버깅하지 못한다 (`.claude/rules/crawling.md`).
"""

from __future__ import annotations

import asyncio
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
from app.crawler.runner import TEST, RunTarget, run_once, run_workflow
from app.selector.schema import validate_selectors

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


def slow_fetcher(
    detail_delay: float,
    *,
    served: int | None = None,
    detail_started: asyncio.Event | None = None,
) -> Fetcher:
    """상세 응답만 늦추는 스텁. 목록까지는 정상적으로 지나간다.

    `served` 를 주면 상세 응답을 그만큼만 바로 주고 그 뒤로는 끝없이 기다린다. 몇 건을 넣은 뒤에
    시간 제한에 걸리는지를 시간(CI 속도)이 아니라 건수로 정하기 위해서다. `detail_started` 는 상세
    요청이 처음 들어왔을 때 켜진다.
    """
    answered = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal answered
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        if request.url.path == "/jobs/":
            return httpx.Response(200, text=LIST_HTML)
        if detail_started is not None:
            detail_started.set()
        if served is not None:
            if answered >= served:
                await asyncio.Event().wait()
            answered += 1
            return httpx.Response(200, text=DETAIL_HTML)
        await asyncio.sleep(detail_delay)
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
    connection.execute(
        "INSERT INTO workflows (crawler_id, name, interval_minutes) VALUES (1, ?, 60)",
        ("python.org 채용",),
    )
    try:
        yield connection
    finally:
        connection.close()


def run_row(conn: sqlite3.Connection) -> sqlite3.Row:
    rows = conn.execute("SELECT * FROM crawl_runs").fetchall()
    assert len(rows) == 1
    return rows[0]


async def test_제한을_넘긴_실행은_timeout_행으로_남는다(conn: sqlite3.Connection) -> None:
    result = await run_workflow(
        conn, 1, fetcher=slow_fetcher(detail_delay=5.0), timeout_seconds=0.2
    )

    assert result.status == "timeout"
    # 느린 사이트인지 목록이 길어진 것인지 모른다. 셋 중 하나로 단정하지 않는다
    assert result.error_class is None
    assert "시간 제한" in result.error_message

    row = run_row(conn)
    assert row["status"] == "timeout"
    assert row["workflow_id"] == 1
    assert row["finished_at"] is not None
    assert "시간 제한" in row["error_message"]


async def test_제한_안에_끝나면_평소대로_success_다(conn: sqlite3.Connection) -> None:
    result = await run_workflow(
        conn, 1, fetcher=slow_fetcher(detail_delay=0.0), limit=2, timeout_seconds=5.0
    )

    assert result.status == "success"
    assert run_row(conn)["status"] == "success"


async def test_제한에_걸려도_그때까지_적재한_것은_남는다(conn: sqlite3.Connection) -> None:
    """`raw_jobs` 는 append-only 다. 끊긴 실행이 이미 넣은 행을 되돌리지 않는다.

    상세 셋만 답하고 넷째부터 멈춘다. 그래서 몇 건을 넣었는지는 CI 속도와 상관없이 셋이고, 제한은
    반드시 걸린다. 제한은 셋을 넣기에 넉넉하게 둔다 — 0.35 초로 두었을 때 느린 CI 에서는 목록만
    읽다가 끊겨 0건이었다.
    """
    result = await run_workflow(
        conn, 1, fetcher=slow_fetcher(detail_delay=0.0, served=3), timeout_seconds=2.0
    )

    assert result.status == "timeout"
    stored = conn.execute("SELECT count(*) AS n FROM raw_jobs").fetchone()["n"]
    assert stored == 3
    assert run_row(conn)["new_count"] == stored


async def test_시간_제한이_없으면_끝까지_돈다(conn: sqlite3.Connection) -> None:
    """항목 수를 정해 놓고 도는 테스트 실행 경로다."""
    result = await run_once(
        conn,
        RunTarget(
            list_url=LIST_URL,
            selectors=validate_selectors(SELECTORS),
            trigger=TEST,
            crawler_id=1,
        ),
        fetcher=slow_fetcher(detail_delay=0.0),
        limit=1,
    )

    assert result.status == "success"
    assert run_row(conn)["status"] == "success"


async def test_밖에서_온_취소도_행을_남기고_올라간다(conn: sqlite3.Connection) -> None:
    """시간 제한이 아니라 프로세스가 내린 취소다. 상태는 timeout 이 아니라 failed 다."""
    started = asyncio.Event()
    task = asyncio.create_task(
        run_workflow(
            conn,
            1,
            fetcher=slow_fetcher(detail_delay=5.0, detail_started=started),
            timeout_seconds=30.0,
        )
    )
    # 실행 행이 생기고 상세를 받는 중에 취소한다.
    # 정해진 시간만 자면 느린 CI 에서는 행이 생기기 전에 취소된다
    async with asyncio.timeout(10.0):
        await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    row = run_row(conn)
    assert row["status"] == "failed"
    assert row["finished_at"] is not None
