"""건너뛴 필드를 성공으로 적지 않는다 (12.4).

2026-08-22 브라우저 QA 에서 `list.company_name`, `detail.qualifications`,
`detail.recruitment_end_at`,
`detail.department` 가 매칭 0개인데 판정이 성공으로 나왔다. 선택 필드라 셀렉터가 비어 판정을
건너뛴 것인데, 화면에는 값을 찾은 필드와 같은 줄로 보였다.

판정은 셋이다.

| 판정 | 뜻 |
|---|---|
| 성공 | 셀렉터가 있고 1개 이상 잡았다 |
| 실패 | 셀렉터가 있는데 0개 잡았다. 손으로 고칠 대상이다 |
| 건너뜀 | 셀렉터가 비어 판정하지 않았다. 고칠 셀렉터가 없다 |

Gemini 도 실사이트도 부르지 않는다. 생성 의존성을 갈아끼우고 저장된 픽스처로 판정한다.
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
LIST_HTML = (FIXTURES / "pythonorg-jobs-list-20260821.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "pythonorg-job-detail-20260821.html").read_text(encoding="utf-8")

LIST_URL = "https://www.python.org/jobs/"
DETAIL_URL = "https://www.python.org/jobs/8126/"

# 목록은 다 잡히고, 선택 필드 일부는 셀렉터가 비어 있다. QA 에서 성공으로 잘못 적히던 모양이다
GENERATED: dict[str, Any] = {
    "list": {
        "item": "ol.list-recent-jobs > li",
        "title": "span.listing-company-name > a",
        "link": "span.listing-company-name > a",
        "date": "span.listing-posted time",
        "company_name": "",
    },
    "detail": {
        "title": "h1.listing-company span.company-name",
        "body": "div.job-description",
        "qualifications": "",
        "recruitment_end_at": "",
        "department": "span.listing-company-category a",
        "company_name": "",
    },
}

USAGE = Usage(
    provider="gemini",
    model="gemini-3.5-flash",
    input_tokens=10399,
    output_tokens=139,
    total_tokens=11229,
    latency_ms=5649,
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


def use_generator(payload: dict[str, Any], detail_html: str = DETAIL_HTML) -> None:
    selectors = validate_selectors(payload)
    result = GenerationResult(
        selectors=selectors,
        usage=USAGE,
        attempts=1,
        verification=verify_selectors(selectors, LIST_HTML, detail_html),
    )

    async def generate(list_url: str, detail_url: str, render_mode: str) -> GenerationResult:
        return result

    app.dependency_overrides[crawlers_api.get_generator] = lambda: generate
    stub_discoverer()


def test_an_empty_optional_selector_is_skipped_not_successful(client: TestClient) -> None:
    """셀렉터가 비어 있으면 건너뜀이다. 실패도 성공도 아니다."""
    use_generator(GENERATED)

    body = client.post(
        "/api/crawlers", json={"list_url": LIST_URL, "detail_url": DETAIL_URL}
    ).json()

    for field in (
        "list.company_name",
        "detail.qualifications",
        "detail.recruitment_end_at",
        "detail.company_name",
    ):
        assert field in body["skipped_fields"], field
        assert field not in body["failed_fields"], field
        assert body["matches"][field] == 0


def test_a_present_selector_matching_nothing_is_a_failure(client: TestClient) -> None:
    """같은 선택 필드라도 셀렉터가 있는데 0개면 실패다. 사이트에 있는데 못 뽑은 것이다."""
    payload = json.loads(json.dumps(GENERATED))
    payload["detail"]["qualifications"] = "div.no-such-requirements"
    use_generator(payload)

    body = client.post(
        "/api/crawlers", json={"list_url": LIST_URL, "detail_url": DETAIL_URL}
    ).json()

    assert "detail.qualifications" in body["failed_fields"]
    assert "detail.qualifications" not in body["skipped_fields"]
    assert body["matches"]["detail.qualifications"] == 0


def test_a_matching_field_stays_successful(client: TestClient) -> None:
    use_generator(GENERATED)

    body = client.post(
        "/api/crawlers", json={"list_url": LIST_URL, "detail_url": DETAIL_URL}
    ).json()

    for field in ("list.item", "list.title", "detail.title", "detail.department"):
        assert body["matches"][field] > 0, field
        assert field not in body["failed_fields"]
        assert field not in body["skipped_fields"]


def test_the_reason_for_the_skip_is_written_down(client: TestClient) -> None:
    """건너뛴 이유가 둘 중 어느 것인지 적혀야 사이트에 없는 항목인지 알 수 있다."""
    use_generator(GENERATED)

    body = client.post(
        "/api/crawlers", json={"list_url": LIST_URL, "detail_url": DETAIL_URL}
    ).json()

    joined = " ".join(body["notes"])
    assert "사이트에 없다고 답해" in joined
    assert "list.company_name" in joined


def test_the_screen_marks_the_three_judgements_as_words(client: TestClient) -> None:
    """화면 판정은 성공·실패·건너뜀 세 단어다. 아이콘을 쓰지 않는다 (rules/writing.md)."""
    payload = json.loads(json.dumps(GENERATED))
    payload["detail"]["qualifications"] = "div.no-such-requirements"
    use_generator(payload)

    html = client.post(
        "/ui/crawlers",
        data={"list_url": LIST_URL, "detail_url": DETAIL_URL, "default_company": "테스트"},
    ).text

    rows = {}
    for chunk in html.split("<tr>")[1:]:
        for field in ("list.item", "list.company_name", "detail.qualifications"):
            if f"<td>{field}</td>" in chunk:
                rows[field] = chunk
    assert set(rows) == {"list.item", "list.company_name", "detail.qualifications"}
    assert "성공" in rows["list.item"]
    assert "건너뜀" in rows["list.company_name"]
    assert "실패" in rows["detail.qualifications"]
    assert "성공" not in rows["list.company_name"]


def test_the_screen_separates_the_skipped_fields_from_the_failed_ones(
    client: TestClient,
) -> None:
    """고칠 필드 목록에 건너뛴 필드가 섞이면 없는 항목을 찾아 헤매게 된다."""
    payload = json.loads(json.dumps(GENERATED))
    payload["detail"]["qualifications"] = "div.no-such-requirements"
    use_generator(payload)

    html = client.post(
        "/ui/crawlers",
        data={"list_url": LIST_URL, "detail_url": DETAIL_URL, "default_company": "테스트"},
    ).text

    fix_line = next(line for line in html.splitlines() if "손으로 고쳐야 하는 필드" in line)
    skip_line = next(line for line in html.splitlines() if "건너뛴 필드" in line)
    assert "detail.qualifications" in html.split("손으로 고쳐야 하는 필드")[1].split("</p>")[0]
    assert "list.company_name" in html.split("건너뛴 필드")[1].split("</p>")[0]
    assert fix_line != skip_line


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
