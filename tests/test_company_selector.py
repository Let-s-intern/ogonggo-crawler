"""셀렉터의 선택 필드 `company_name` 테스트.

확인하는 것은 셋이다.

- `company_name` 가 없는, 이 필드가 생기기 전의 셀렉터 JSON 이 그대로 통과한다
- `company_name` 가 있는 셀렉터 JSON 도 통과한다
- 그 셀렉터로 크롤링하면 뽑힌 회사명이 `raw_jobs.raw_data_json` 에 들어간다

계열사가 섞인 목록 픽스처로 돈다. 실사이트에 나가지 않는다.
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
from app.crawler.parser import parse_detail, parse_list
from app.crawler.runner import run_workflow
from app.selector.schema import SelectorSchemaError, validate_selectors
from app.selector.verify import SKIPPED, verify_selectors

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
LIST_HTML = (FIXTURES / "affiliates-list-20260822.html").read_text(encoding="utf-8")
DETAIL_HTML = {
    "/recruit/1001": (FIXTURES / "affiliates-detail-1001-20260822.html").read_text(
        encoding="utf-8"
    ),
    "/recruit/1002": (FIXTURES / "affiliates-detail-1002-20260822.html").read_text(
        encoding="utf-8"
    ),
}

LIST_URL = "https://group.example.test/recruit/"
ROBOTS = "User-agent: *\nDisallow:\n"

# 이 필드가 생기기 전에 저장된 모양. 목록·상세 어디에도 `company_name` 키가 없다
WITHOUT_COMPANY: dict[str, Any] = {
    "list": {
        "item": "ul.job-list > li.job-item",
        "title": "a.job-link",
        "link": "a.job-link",
        "date": "span.posted",
    },
    "detail": {
        "title": "h1.job-title",
        "body": "div.job-body",
        "qualifications": "div.job-requirements",
        "recruitment_end_at": "span.due",
        "department": "span.dept",
    },
}

WITH_COMPANY: dict[str, Any] = {
    "list": {**WITHOUT_COMPANY["list"], "company_name": "span.affiliate"},
    "detail": {**WITHOUT_COMPANY["detail"], "company_name": "span.company-name"},
}


def stub_fetcher() -> Fetcher:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        if request.url.path == "/recruit/":
            return httpx.Response(200, text=LIST_HTML)
        return httpx.Response(200, text=DETAIL_HTML[request.url.path])

    async def no_wait(seconds: float) -> None:
        return None

    return Fetcher(
        settings=Settings(crawl_delay_seconds=0.0, crawl_max_retries=0),
        transport=httpx.MockTransport(handle),
        sleep=no_wait,
    )


def make_conn(
    path: pathlib.Path, selectors: dict[str, Any], default_company: str | None = None
) -> sqlite3.Connection:
    """크롤러 하나와 워크플로우 하나가 있는 DB. 회사명 해결 테스트가 함께 쓴다."""
    connection = db.connect(path)
    db.migrate_up(connection)
    connection.execute(
        """
        INSERT INTO crawlers (name, list_url, selectors_json, status, default_company)
        VALUES (?, ?, ?, 'promoted', ?)
        """,
        ("그룹 채용", LIST_URL, json.dumps(selectors), default_company),
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '그룹 채용')")
    return connection


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = make_conn(tmp_path / "jobs.db", WITH_COMPANY)
    try:
        yield connection
    finally:
        connection.close()


def raw_rows(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute("SELECT raw_data_json FROM raw_jobs ORDER BY id").fetchall()
    return [json.loads(row["raw_data_json"]) for row in rows]


def test_selectors_saved_before_company_existed_still_validate() -> None:
    """키가 아예 없어도 통과한다. 통과하지 않으면 저장된 셀렉터가 전부 깨진다."""
    selectors = validate_selectors(WITHOUT_COMPANY)

    assert selectors.list.company_name == ""
    assert selectors.detail.company_name == ""


def test_selectors_with_company_validate() -> None:
    selectors = validate_selectors(WITH_COMPANY)

    assert selectors.list.company_name == "span.affiliate"
    assert selectors.detail.company_name == "span.company-name"


def test_the_other_fields_are_still_required() -> None:
    """`company_name` 만 선택이다. 값이 비어도 되는 상세 필드조차 키는 있어야 한다."""
    payload = json.loads(json.dumps(WITH_COMPANY))
    del payload["detail"]["department"]

    with pytest.raises(SelectorSchemaError) as caught:
        validate_selectors(payload)

    assert caught.value.reason == "missing_field"


def test_an_empty_company_selector_is_not_a_failed_field() -> None:
    """회사명이 페이지에 없다는 응답이다. 실패로 세면 운영자가 고칠 것이 없는데 고치라고 나온다."""
    report = verify_selectors(
        validate_selectors(WITHOUT_COMPANY), LIST_HTML, DETAIL_HTML["/recruit/1001"]
    )

    by_name = {field.name: field for field in report.fields}
    assert by_name["list.company_name"].status == SKIPPED
    assert by_name["detail.company_name"].status == SKIPPED
    assert report.failed == []


def test_parser_reads_a_different_company_per_item() -> None:
    selectors = validate_selectors(WITH_COMPANY)

    parsed = parse_list(LIST_HTML, selectors.list, LIST_URL)

    assert [item.company_name for item in parsed.items] == ["삼성SDS", "삼성전기(주)"]


def test_parser_leaves_company_empty_without_a_selector() -> None:
    selectors = validate_selectors(WITHOUT_COMPANY)

    parsed = parse_list(LIST_HTML, selectors.list, LIST_URL)
    detail = parse_detail(DETAIL_HTML["/recruit/1001"], selectors.detail)

    assert [item.company_name for item in parsed.items] == ["", ""]
    assert detail.fields["company_name"] == ""


async def test_parsed_company_reaches_raw_data_json(conn: sqlite3.Connection) -> None:
    """파싱값은 다른 필드와 똑같은 추출 결과다. raw 에 그대로 들어간다."""
    await run_workflow(conn, 1, fetcher=stub_fetcher(), limit=2)

    assert [row["company_name"] for row in raw_rows(conn)] == ["삼성SDS", "삼성전기(주)"]


async def test_raw_data_json_has_an_empty_company_without_a_selector(
    tmp_path: pathlib.Path,
) -> None:
    """셀렉터가 없으면 빈 문자열이다. 운영자 입력은 여기에 들어오지 않는다."""
    connection = make_conn(tmp_path / "jobs.db", WITHOUT_COMPANY, default_company="삼성전자")
    try:
        await run_workflow(connection, 1, fetcher=stub_fetcher(), limit=2)

        assert [row["company_name"] for row in raw_rows(connection)] == ["", ""]
    finally:
        connection.close()
