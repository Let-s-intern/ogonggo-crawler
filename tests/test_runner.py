"""1회 실행 러너 테스트.

실사이트에 나가지 않는다. 저장된 python.org 픽스처를 `httpx.MockTransport` 로 돌려주고,
DB 는 임시 파일에 마이그레이션을 올려 쓴다.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import httpx
import pytest

from app import db
from app.config import Settings
from app.crawler.fetcher import Fetcher
from app.crawler.runner import (
    KNOWN,
    PREVIEW,
    SCHEDULE,
    STORED,
    TEST,
    RunTarget,
    run_once,
)
from app.selector.schema import SelectorSet, validate_selectors

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
LIST_HTML = (FIXTURES / "pythonorg-jobs-list-20260821.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "pythonorg-job-detail-20260821.html").read_text(encoding="utf-8")

LIST_URL = "https://www.python.org/jobs/"
FIRST_DETAIL_URL = "https://www.python.org/jobs/8126/"
ROBOTS = "User-agent: *\nDisallow:\n"

SELECTORS = validate_selectors(
    {
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
)


class StubSite:
    """픽스처를 돌려주는 스텁. 어떤 URL 을 몇 번 요청했는지 기록한다."""

    def __init__(self, *, list_status: int = 200, detail_status: int = 200) -> None:
        self.list_status = list_status
        self.detail_status = detail_status
        self.requests: list[str] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        self.requests.append(str(request.url))
        if path == "/jobs/":
            if self.list_status != 200:
                return httpx.Response(self.list_status)
            return httpx.Response(200, text=LIST_HTML)
        if self.detail_status != 200 and not path.endswith("8125/"):
            return httpx.Response(self.detail_status)
        return httpx.Response(200, text=DETAIL_HTML)

    async def sleep(self, seconds: float) -> None:
        return None

    def fetcher(self) -> Fetcher:
        return Fetcher(
            settings=Settings(crawl_delay_seconds=0.0, crawl_max_retries=0),
            transport=self.transport,
            sleep=self.sleep,
        )


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES (?, ?, 'draft')",
        ("python.org", LIST_URL),
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, 'python.org 채용')")
    try:
        yield connection
    finally:
        connection.close()


def workflow_target(selectors: SelectorSet = SELECTORS) -> RunTarget:
    return RunTarget(list_url=LIST_URL, selectors=selectors, trigger=SCHEDULE, workflow_id=1)


def rows(connection: sqlite3.Connection, sql: str) -> list[sqlite3.Row]:
    return connection.execute(sql).fetchall()


def replaced(section: str, **changes: str) -> SelectorSet:
    payload = SELECTORS.model_dump()
    payload[section].update(changes)
    return validate_selectors(payload)


async def test_같은_픽스처로_두_번_실행해도_raw_jobs_는_한_행이다(
    conn: sqlite3.Connection,
) -> None:
    site = StubSite()
    fetcher = site.fetcher()

    first = await run_once(conn, workflow_target(), fetcher=fetcher, limit=1)

    assert first.status == "success"
    assert (first.success_count, first.new_count, first.fail_count) == (1, 1, 0)
    assert [item.state for item in first.items] == [STORED]
    stored = rows(conn, "SELECT * FROM raw_jobs")
    assert len(stored) == 1
    assert site.requests == [LIST_URL, FIRST_DETAIL_URL]

    second = await run_once(conn, workflow_target(), fetcher=fetcher, limit=1)
    await fetcher.aclose()

    assert second.status == "success"
    assert (second.success_count, second.new_count, second.fail_count) == (1, 0, 0)
    assert [item.state for item in second.items] == [KNOWN]

    after = rows(conn, "SELECT * FROM raw_jobs")
    assert len(after) == 1
    # append-only. 기존 행을 다시 쓰지 않는다
    assert dict(after[0]) == dict(stored[0])
    # 아는 공고라 상세를 다시 따라가지 않는다
    assert site.requests == [LIST_URL, FIRST_DETAIL_URL, LIST_URL]

    runs = rows(conn, "SELECT * FROM crawl_runs ORDER BY id")
    assert len(runs) == 2
    assert [run["status"] for run in runs] == ["success", "success"]
    assert [run["new_count"] for run in runs] == [1, 0]
    assert all(run["finished_at"] is not None for run in runs)
    assert all(run["workflow_id"] == 1 for run in runs)


async def test_적재된_값은_셀렉터가_뽑은_그대로다(conn: sqlite3.Connection) -> None:
    fetcher = StubSite().fetcher()

    await run_once(conn, workflow_target(), fetcher=fetcher, limit=1)
    await fetcher.aclose()

    row = rows(conn, "SELECT * FROM raw_jobs")[0]
    record = json.loads(row["raw_data_json"])

    assert row["source_url"] == FIRST_DETAIL_URL
    assert record["source_url"] == FIRST_DETAIL_URL
    assert "Software Engineer (Remote)" in record["title"]
    assert "Join Softech Associate" in record["body"]
    assert record["list_date"] == "16 August 2026"
    assert len(row["content_hash"]) == 64


async def test_item_0개_매칭은_실패로_남는다(conn: sqlite3.Connection) -> None:
    """가져오기는 200 이었다. 신규 0건인 정상 실행으로 남기지 않는다."""
    fetcher = StubSite().fetcher()
    target = RunTarget(
        list_url=LIST_URL,
        selectors=replaced("list", item="ol.list-of-nothing > li"),
        trigger=SCHEDULE,
        workflow_id=1,
    )

    result = await run_once(conn, target, fetcher=fetcher, limit=1)
    await fetcher.aclose()

    assert result.status == "failed"
    assert result.error_class == "selector_miss"
    assert (result.success_count, result.new_count) == (0, 0)

    run = rows(conn, "SELECT * FROM crawl_runs")[0]
    assert run["status"] == "failed"
    assert run["error_class"] == "selector_miss"
    assert run["error_message"]
    assert rows(conn, "SELECT * FROM raw_jobs") == []


async def test_목록을_못_가져오면_transport_로_남는다(conn: sqlite3.Connection) -> None:
    fetcher = StubSite(list_status=503).fetcher()

    result = await run_once(conn, workflow_target(), fetcher=fetcher, limit=1)
    await fetcher.aclose()

    assert result.status == "failed"
    assert result.error_class == "transport"

    run = rows(conn, "SELECT * FROM crawl_runs")[0]
    assert run["status"] == "failed"
    assert run["error_class"] == "transport"
    assert run["finished_at"] is not None


async def test_상세_하나가_실패해도_나머지는_적재된다(conn: sqlite3.Connection) -> None:
    """8125 만 정상이고 나머지 상세는 500 을 돌려준다."""
    site = StubSite(detail_status=500)
    fetcher = site.fetcher()

    result = await run_once(conn, workflow_target(), fetcher=fetcher, limit=2)
    await fetcher.aclose()

    assert (result.success_count, result.new_count, result.fail_count) == (1, 1, 1)
    assert result.status == "success"
    assert [failure.error_class for failure in result.failures] == ["transport"]
    assert len(rows(conn, "SELECT * FROM raw_jobs")) == 1


async def test_상세_필드를_못_읽으면_parse_로_남는다(conn: sqlite3.Connection) -> None:
    fetcher = StubSite().fetcher()
    target = RunTarget(
        list_url=LIST_URL,
        # 제목까지 깨뜨린다. 제목이 읽히면 본문은 제목 근처의 글로 대신 채운다
        selectors=replaced("detail", title="h1.no-such-title", body="div.no-such-description"),
        trigger=SCHEDULE,
        workflow_id=1,
    )

    result = await run_once(conn, target, fetcher=fetcher, limit=1)
    await fetcher.aclose()

    assert result.status == "failed"
    assert result.error_class == "parse"
    assert result.fail_count == 1

    run = rows(conn, "SELECT * FROM crawl_runs")[0]
    assert run["error_class"] == "parse"
    assert rows(conn, "SELECT * FROM raw_jobs") == []


async def test_워크플로우_없는_실행은_적재하지_않고_행만_남긴다(
    conn: sqlite3.Connection,
) -> None:
    fetcher = StubSite().fetcher()
    target = RunTarget(list_url=LIST_URL, selectors=SELECTORS, trigger=TEST, crawler_id=1)

    result = await run_once(conn, target, fetcher=fetcher, limit=2)
    await fetcher.aclose()

    assert result.status == "success"
    assert [item.state for item in result.items] == [PREVIEW, PREVIEW]
    assert (result.success_count, result.new_count) == (2, 0)
    assert rows(conn, "SELECT * FROM raw_jobs") == []

    run = rows(conn, "SELECT * FROM crawl_runs")[0]
    assert run["crawler_id"] == 1
    assert run["workflow_id"] is None
    assert run["status"] == "success"


def test_어디에도_속하지_않는_실행은_만들지_못한다() -> None:
    with pytest.raises(ValueError, match="workflow_id"):
        RunTarget(list_url=LIST_URL, selectors=SELECTORS, trigger=SCHEDULE)
