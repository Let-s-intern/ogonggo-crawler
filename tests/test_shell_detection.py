"""껍데기 감지와 승격 안내 테스트.

픽스처만 쓴다. 확인하는 것은 두 가지다. 껍데기 페이지의 0개 매칭이 승격 안내를 달고
`crawl_runs.error_message` 에 남는가, 그리고 정상 목록 페이지는 그 안내를 달지 않는가.

정상 목록인데 0개 매칭인 경우는 사이트가 마크업을 바꾼 것이고, 그때 렌더 모드를 권하면
운영자가 브라우저를 띄워 놓고 같은 실패를 다시 본다.
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
from app.crawler.shell import PROMOTION_NOTICE, inspect_static_html, promotion_hint

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SHELL_HTML = (FIXTURES / "js-rendered-list-shell-20260822.html").read_text(encoding="utf-8")
LIST_HTML = (FIXTURES / "pythonorg-jobs-list-20260821.html").read_text(encoding="utf-8")

LIST_URL = "https://www.python.org/jobs/"
ROBOTS = "User-agent: *\nDisallow:\n"

# 어느 페이지에서도 잡히지 않는 셀렉터. 0개 매칭을 만드는 것이 목적이다
SELECTORS: dict[str, Any] = {
    "list": {
        "item": "ul#applyList > li.item",
        "title": "a.tit",
        "link": "a",
        "date": "span.date",
    },
    "detail": {
        "title": "h1",
        "body": "div.body",
        "qualifications": "",
        "recruitment_end_at": "",
        "department": "",
    },
}


def stub_fetcher(html: str) -> Fetcher:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        return httpx.Response(200, text=html)

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


def add_workflow(conn: sqlite3.Connection, render_mode: str = "static") -> int:
    conn.execute(
        """
        INSERT INTO crawlers (name, list_url, selectors_json, status, list_mode, detail_mode)
        VALUES (?, ?, ?, 'promoted', ?, ?)
        """,
        ("대상", LIST_URL, json.dumps(SELECTORS), render_mode, render_mode),
    )
    cursor = conn.execute(
        "INSERT INTO workflows (crawler_id, name, interval_minutes) VALUES (1, ?, 360)",
        ("대상 채용",),
    )
    return int(cursor.lastrowid or 0)


def test_a_shell_page_is_measured_as_a_shell() -> None:
    verdict = inspect_static_html(SHELL_HTML)

    assert verdict.repeating_items < 3
    assert verdict.is_shell is True


def test_empty_repeated_elements_are_not_a_list() -> None:
    """스크립트가 채울 빈 껍데기가 반복되는 사이트가 있다(한화 실측 10개).

    글자 없는 반복을 목록으로 세면 본문 3자짜리 페이지가 목록 있는 페이지로 판정된다.
    """
    skeleton = (
        "<html><body><div id='wrap'>" + "<div class='item'></div>" * 10 + "로딩</div></body></html>"
    )

    verdict = inspect_static_html(skeleton)

    assert verdict.repeating_items == 0
    assert verdict.is_shell is True


def test_a_real_list_page_is_not_a_shell() -> None:
    verdict = inspect_static_html(LIST_HTML)

    assert verdict.repeating_items >= 3
    assert verdict.is_shell is False


def test_a_render_mode_crawler_gets_no_promotion_hint() -> None:
    """이미 렌더로 도는데 0개 매칭이면 셀렉터 문제다. 승격을 다시 권하지 않는다."""
    assert promotion_hint(SHELL_HTML, "playwright") is None
    assert promotion_hint(SHELL_HTML, "static") is not None


async def test_a_shell_run_fails_with_the_promotion_hint(conn: sqlite3.Connection) -> None:
    workflow_id = add_workflow(conn)

    result = await run_workflow(conn, workflow_id, fetcher=stub_fetcher(SHELL_HTML), limit=1)

    assert result.status == "failed"
    assert result.error_class == "selector_miss"
    assert PROMOTION_NOTICE in result.error_message

    row = conn.execute("SELECT * FROM crawl_runs WHERE id = ?", (result.run_id,)).fetchone()
    assert row["status"] == "failed"
    assert row["error_class"] == "selector_miss"
    assert PROMOTION_NOTICE in row["error_message"]
    # 본 것을 숫자로 남긴다. 사유만 있으면 왜 그렇게 판정했는지 되짚을 수 없다
    assert "반복 항목 0개" in row["error_message"]


async def test_a_changed_markup_run_fails_without_the_hint(conn: sqlite3.Connection) -> None:
    """목록이 있는 페이지의 0개 매칭은 마크업 변경이다. 렌더 모드를 권하지 않는다."""
    workflow_id = add_workflow(conn)

    result = await run_workflow(conn, workflow_id, fetcher=stub_fetcher(LIST_HTML), limit=1)

    assert result.status == "failed"
    assert result.error_class == "selector_miss"
    assert PROMOTION_NOTICE not in result.error_message


async def test_a_shell_run_does_not_switch_the_mode_by_itself(conn: sqlite3.Connection) -> None:
    """안내만 한다. 승격은 운영자가 정한다."""
    workflow_id = add_workflow(conn)

    await run_workflow(conn, workflow_id, fetcher=stub_fetcher(SHELL_HTML), limit=1)

    row = conn.execute("SELECT list_mode FROM crawlers WHERE id = 1").fetchone()
    assert row["list_mode"] == "static"
