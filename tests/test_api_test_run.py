"""테스트 실행 API 테스트.

실사이트에 나가지 않는다. fetch 클라이언트 의존성을 저장된 python.org 픽스처를 돌려주는
스텁으로 갈아끼우고, 확인하는 것은 응답과 `crawl_runs` 행, 그리고 `crawlers.status` 다.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import crawlers as crawlers_api
from app.config import Settings
from app.crawler.fetcher import Fetcher
from app.main import app

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


@pytest.fixture
def client(tmp_path: pathlib.Path, conn: sqlite3.Connection) -> Iterator[TestClient]:
    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(tmp_path / "jobs.db")
        try:
            yield connection
        finally:
            connection.close()

    fetcher = stub_fetcher()
    app.dependency_overrides[crawlers_api.get_connection] = request_connection
    app.dependency_overrides[crawlers_api.get_crawl_fetcher] = lambda: fetcher
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def add_crawler(conn: sqlite3.Connection, selectors: dict[str, Any] | None) -> int:
    cursor = conn.execute(
        """
        INSERT INTO crawlers (name, list_url, detail_url, selectors_json, status)
        VALUES (?, ?, ?, ?, 'draft')
        """,
        (
            "python.org",
            LIST_URL,
            "https://www.python.org/jobs/8126/",
            json.dumps(selectors) if selectors is not None else None,
        ),
    )
    return int(cursor.lastrowid or 0)


def crawler_status(conn: sqlite3.Connection, crawler_id: int) -> str:
    row = conn.execute("SELECT status FROM crawlers WHERE id = ?", (crawler_id,)).fetchone()
    return str(row["status"])


def runs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM crawl_runs").fetchall()


def test_통과한_테스트_실행은_tested_로_올린다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    crawler_id = add_crawler(conn, SELECTORS)

    response = client.post(f"/api/crawlers/{crawler_id}/test-run?limit=2")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["matched"] == 25
    assert (body["success_count"], body["fail_count"]) == (2, 0)
    # 적재하지 않는 실행이라 신규는 0 이다
    assert body["new_count"] == 0
    assert body["crawler_status"] == "tested"
    assert crawler_status(conn, crawler_id) == "tested"

    assert len(body["items"]) == 2
    first = body["items"][0]
    assert first["state"] == "preview"
    assert "Software Engineer (Remote)" in first["fields"]["title"]

    stored = runs(conn)
    assert len(stored) == 1
    assert stored[0]["crawler_id"] == crawler_id
    assert stored[0]["workflow_id"] is None
    assert stored[0]["status"] == "success"
    # 테스트 실행은 수집 데이터를 남기지 않는다
    assert conn.execute("SELECT count(*) AS n FROM raw_jobs").fetchone()["n"] == 0


def test_실패한_테스트_실행은_상태를_올리지_않는다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    broken = json.loads(json.dumps(SELECTORS))
    broken["list"]["item"] = "ol.list-of-nothing > li"
    crawler_id = add_crawler(conn, broken)

    response = client.post(f"/api/crawlers/{crawler_id}/test-run")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["error_class"] == "selector_miss"
    assert body["error_message"]
    assert body["items"] == []
    assert body["crawler_status"] == "draft"
    assert crawler_status(conn, crawler_id) == "draft"
    assert runs(conn)[0]["status"] == "failed"


def test_필드_하나만_실패하면_사유가_필드와_함께_온다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    broken = json.loads(json.dumps(SELECTORS))
    broken["detail"]["body"] = "div.no-such-description"
    crawler_id = add_crawler(conn, broken)

    body = client.post(f"/api/crawlers/{crawler_id}/test-run?limit=1").json()

    assert body["status"] == "failed"
    assert body["error_class"] == "parse"
    assert "body" in body["failures"][0]["message"]
    assert crawler_status(conn, crawler_id) == "draft"


def test_없는_크롤러는_404_다(client: TestClient) -> None:
    assert client.post("/api/crawlers/999/test-run").status_code == 404


def test_셀렉터가_없으면_실행하지_않는다(client: TestClient, conn: sqlite3.Connection) -> None:
    crawler_id = add_crawler(conn, None)

    response = client.post(f"/api/crawlers/{crawler_id}/test-run")

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "no_selectors"
    assert runs(conn) == []
