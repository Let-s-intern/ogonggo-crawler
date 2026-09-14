"""항목은 잡혔는데 그 안이 빈 크롤러는 거절한다 (12.5).

2026-08-22 브라우저 QA 에서 나왔다. `list.item` 이 1개 잡혀 12.2 의 "목록 필드가 전부 0개"
조건을 빠져나갔고, `list.title`·`list.link`·`list.date` 는 전부 0인 채로 201 로 저장됐다.
제목도 링크도 날짜도 없는 공고만 나오는 크롤러라 실행해도 쓸 값이 하나도 안 나온다.

**항목 개수로는 판정하지 않는다.** 공고가 진짜 1건인 목록 페이지가 있고 그것은 정상이다.
판정에 쓰는 것은 항목 안의 세 필드가 전부 0인가 하나뿐이다.

Gemini 도 실사이트도 부르지 않는다. 저장된 픽스처 둘로 판정한다.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import crawlers as crawlers_api
from app.main import app
from app.selector.detail_path import document_path
from app.selector.discovery import Discovery
from app.selector.generator import GenerationResult, Usage
from app.selector.schema import SelectorSet, validate_selectors
from app.selector.verify import verify_selectors

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
# 공고가 진짜 1건인 목록. 항목 수로 판정하면 이 정상 페이지가 거절된다
SINGLE_HTML = (FIXTURES / "single-posting-list-20260822.html").read_text(encoding="utf-8")
# 목록 컨테이너만 있고 항목은 스크립트가 채우는 사이트
SHELL_HTML = (FIXTURES / "js-rendered-list-shell-20260822.html").read_text(encoding="utf-8")

LIST_URL = "https://example.co.kr/recruit/"
DETAIL_URL = "https://example.co.kr/recruit/view/2026-0001"

# 1건짜리 목록을 제대로 잡은 셀렉터
SINGLE_GENERATED: dict[str, Any] = {
    "list": {
        "item": "ul.recruit-list > li",
        "title": "h4.recruit-title",
        "link": "h4.recruit-title a",
        "date": "span.recruit-date",
    },
    "detail": {
        "title": "h1.tit",
        "body": "#container",
        "qualifications": "",
        "recruitment_end_at": "",
        "department": "",
    },
}

# 껍데기 페이지를 보고 컨테이너를 반복 단위로 잘못 잡은 셀렉터. `#container` 는 1개 잡히고
# 그 안에서는 아무것도 안 나온다 — QA 에서 201 로 저장되던 모양이다
CONTAINER_AS_ITEM: dict[str, Any] = {
    "list": {
        "item": "#container",
        "title": "li .tit_job",
        "link": "li a.job-link",
        "date": "li .date",
    },
    "detail": {
        "title": "h1.tit",
        "body": "#container",
        "qualifications": "",
        "recruitment_end_at": "",
        "department": "",
    },
}

USAGE = Usage(
    provider="gemini",
    model="gemini-3.5-flash",
    input_tokens=8112,
    output_tokens=141,
    total_tokens=8253,
    latency_ms=4310,
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

    app.dependency_overrides[crawlers_api.get_connection] = request_connection
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def use_generator(payload: dict[str, Any], list_html: str) -> None:
    selectors = validate_selectors(payload)
    result = GenerationResult(
        selectors=selectors,
        usage=USAGE,
        attempts=1,
        verification=verify_selectors(selectors, list_html, list_html),
    )

    async def generate(list_url: str, detail_url: str, render_mode: str) -> GenerationResult:
        return result

    app.dependency_overrides[crawlers_api.get_generator] = lambda: generate
    stub_discoverer()


def rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM crawlers").fetchall()


def register(client: TestClient) -> Any:
    return client.post("/api/crawlers", json={"list_url": LIST_URL, "detail_url": DETAIL_URL})


def test_an_item_with_no_field_inside_is_refused(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """항목 1개를 잡고도 제목·링크·날짜가 전부 0이면 못 쓰는 크롤러다. 행도 남지 않는다."""
    use_generator(CONTAINER_AS_ITEM, SHELL_HTML)

    response = register(client)

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "list_fields_not_found"
    assert response.json()["detail"]["matches"]["list.item"] == 1
    assert rows(conn) == []


def test_the_refusal_says_the_items_were_found(client: TestClient) -> None:
    """12.2 와 사유가 다르다. 목록은 찾았고 그 안이 비었다는 뜻이라 다음 수단도 다르다."""
    use_generator(CONTAINER_AS_ITEM, SHELL_HTML)

    detail = register(client).json()["detail"]

    assert "목록 항목은 찾았으나 그 안에서 필드를 뽑지 못했다" in detail["message"]
    assert "정적 HTML 에서 목록을 찾지 못했다" not in detail["message"]
    for field in ("list.title", "list.link", "list.date"):
        assert field in detail["message"]


def test_a_list_with_one_real_posting_is_stored(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """공고가 진짜 1건인 목록은 정상이다. 항목 개수로 판정하지 않는다."""
    use_generator(SINGLE_GENERATED, SINGLE_HTML)

    response = register(client)

    assert response.status_code == 201
    assert response.json()["matches"]["list.item"] == 1
    assert response.json()["failed_fields"] == []
    assert len(rows(conn)) == 1


def test_one_matching_field_keeps_the_draft(client: TestClient, conn: sqlite3.Connection) -> None:
    """항목 1개에 제목만 잡혀도 저장한다. 나머지는 손으로 고칠 대상이라 이름을 적는다."""
    payload = json.loads(json.dumps(SINGLE_GENERATED))
    payload["list"]["link"] = "a.no-such-link"
    payload["list"]["date"] = "span.no-such-date"
    use_generator(payload, SINGLE_HTML)

    response = register(client)

    assert response.status_code == 201
    assert response.json()["failed_fields"] == ["list.link", "list.date"]
    assert response.json()["matches"]["list.title"] == 1
    assert len(rows(conn)) == 1


def test_a_matching_company_does_not_rescue_an_empty_item(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """계열사 이름 하나가 잡혀도 제목도 링크도 날짜도 없으면 쓸 수 없다."""
    payload = json.loads(json.dumps(SINGLE_GENERATED))
    payload["list"]["company_name"] = "h1.tit"
    payload["list"]["title"] = "h4.no-such-title"
    payload["list"]["link"] = "a.no-such-link"
    payload["list"]["date"] = "span.no-such-date"
    use_generator(payload, SINGLE_HTML)

    response = register(client)

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "list_fields_not_found"
    assert rows(conn) == []


def test_an_empty_list_is_still_the_other_failure(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """항목까지 0개면 12.2 의 사유 그대로다. 두 실패가 섞이지 않는다."""
    payload = json.loads(json.dumps(CONTAINER_AS_ITEM))
    payload["list"]["item"] = "#applyList li"
    use_generator(payload, SHELL_HTML)

    response = register(client)

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "list_not_found"
    assert rows(conn) == []


def stub_discoverer() -> None:
    """경로 판정도 갈아끼운다. 기본 경로는 실사이트를 다시 가져오고 브라우저까지 연다.

    등록은 셀렉터 생성 다음에 상세로 가는 길을 알아본다 (`app/api/crawlers.py` 의
    `create_crawler`). 여기서 갈아끼우지 않으면 이 테스트가 네트워크에 매달린다
    (`.claude/rules/core.md`).
    """

    async def discover(list_url: str, selectors: SelectorSet) -> Discovery:
        return Discovery(
            list_mode="static",
            detail_mode="static",
            detail=document_path(f"{list_url}1/", "목록 항목의 링크를 그대로 따라간다"),
            evidence="정적 목록에서 항목과 상세 주소를 찾았다. 브라우저를 띄우지 않았다",
            list_count=1,
        )

    app.dependency_overrides[crawlers_api.get_discoverer] = lambda: discover
