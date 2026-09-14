"""새 크롤러가 어느 모드로 저장되는가.

Gemini 도 실사이트도 브라우저도 부르지 않는다. 생성 의존성을 갈아끼우고, 확인하는 것은
`crawlers.list_mode` 에 무엇이 저장됐는가와 생성이 어느 모드로 불렸는가다.

| 확인 | 근거 |
|---|---|
| 값을 안 준 등록은 생성에 빈 값을 넘긴다 | 등록이 스스로 정한다. 운영자에게 묻지 않는다 |
| `playwright` 를 명시한 등록은 그대로 | 고른 값을 판정이 덮어쓰지 않는다 |
| 등록 화면에는 모드 입력이 없다 | 화면과 API 의 기본이 갈리면 안 된다 |
| 이미 있는 행은 안 바뀐다 | 등록 하나가 다른 크롤러를 끌어내리지 않는다 |

빈 값을 받은 생성이 실제로 정적에서 렌더로 올라가는 것은
`tests/test_register_escalates.py` 가 본다. 어느 모드가 필요한지 비교하는 테스트 실행 화면은
`tests/test_test_run_mode.py` 다.
"""

from __future__ import annotations

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

TEMPLATES = pathlib.Path(__file__).parent.parent / "app" / "templates"

LIST_URL = "https://www.python.org/jobs/"
DETAIL_URL = "https://www.python.org/jobs/8126/"

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


class Verified:
    """검증 결과 대역. 전부 잡힌 생성이다.

    `app/selector/verify.py` 를 부르지 않는다. 여기서 보는 것은 어느 모드가 저장되는가이고,
    셀렉터가 몇 개 잡혔는지는 그 모듈의 테스트가 따로 본다.
    """

    list_missing = False
    failed: list[str] = []
    failed_list_fields: list[str] = []
    # 셀렉터가 비어 판정을 건너뛴 필드는 없다 (`app/selector/verify.py`)
    skipped: list[str] = []
    # 항목 안의 필드도 잡혔다. 12.5 의 거절 대상이 아니다
    list_fields_missing = False

    def summary(self) -> dict[str, int]:
        return {"list.item": 25}


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


@pytest.fixture
def called_with() -> list[str]:
    """생성이 어떤 `render_mode` 로 불렸는지 쌓인다."""
    modes: list[str] = []

    async def generate(list_url: str, detail_url: str, render_mode: str) -> GenerationResult:
        modes.append(render_mode)
        return GenerationResult(
            selectors=validate_selectors(SELECTORS),
            usage=Usage(
                provider="gemini",
                model="gemini-3.5-flash",
                input_tokens=10399,
                output_tokens=139,
                total_tokens=11229,
                latency_ms=5649,
            ),
            attempts=1,
            verification=Verified(),  # type: ignore[arg-type]
        )

    app.dependency_overrides[crawlers_api.get_generator] = lambda: generate
    stub_discoverer()
    return modes


def modes(conn: sqlite3.Connection) -> list[str]:
    return [str(row["list_mode"]) for row in conn.execute("SELECT list_mode FROM crawlers")]


def test_등록에_모드를_안_주면_생성이_스스로_정한다(
    client: TestClient, conn: sqlite3.Connection, called_with: list[str]
) -> None:
    """빈 값이 그대로 생성에 넘어가야 정적으로 먼저 해 보고 안 되면 렌더로 올릴 수 있다."""
    response = client.post("/api/crawlers", json={"list_url": LIST_URL, "detail_url": DETAIL_URL})

    assert response.status_code == 201
    # 대역은 정적으로 만들었다고 답한다. 저장되는 값은 생성이 실제로 쓴 경로다
    assert response.json()["render_mode"] == "static"
    assert modes(conn) == ["static"]
    assert called_with == [""]


def test_빈_문자열도_안_고른_것으로_읽는다(
    client: TestClient, conn: sqlite3.Connection, called_with: list[str]
) -> None:
    """화면이 값을 못 실어 보낸 경우다. 안 고른 것이므로 판정에 맡긴다."""
    response = client.post(
        "/api/crawlers",
        json={"list_url": LIST_URL, "detail_url": DETAIL_URL, "render_mode": "  "},
    )

    assert response.status_code == 201
    assert modes(conn) == ["static"]


def test_렌더를_명시한_등록은_렌더로_저장된다(
    client: TestClient, conn: sqlite3.Connection, called_with: list[str]
) -> None:
    """정적으로 목록이 안 나오는 것을 아는 사이트다. 고르면 생성도 렌더된 HTML 을 본다."""
    response = client.post(
        "/api/crawlers",
        json={"list_url": LIST_URL, "detail_url": DETAIL_URL, "render_mode": "playwright"},
    )

    assert response.status_code == 201
    assert response.json()["render_mode"] == "playwright"
    assert modes(conn) == ["playwright"]
    assert called_with == ["playwright"]


def test_이미_있는_행은_새_기본값에_끌려가지_않는다(
    client: TestClient, conn: sqlite3.Connection, called_with: list[str]
) -> None:
    """기본값은 새 등록에만 걸린다. 이미 올려 둔 크롤러를 등록 하나가 끌어내리면 안 된다."""
    conn.execute(
        "INSERT INTO crawlers (name, list_url, list_mode, detail_mode) "
        "VALUES ('기존', ?, 'playwright', 'playwright')",
        (LIST_URL,),
    )
    conn.commit()

    client.post("/api/crawlers", json={"list_url": LIST_URL, "detail_url": DETAIL_URL})
    db.migrate_up(conn)

    saved = conn.execute("SELECT list_mode FROM crawlers WHERE name = '기존'").fetchone()
    assert saved["list_mode"] == "playwright"


def test_화면_경로도_같은_기본값을_쓴다(
    client: TestClient, conn: sqlite3.Connection, called_with: list[str]
) -> None:
    """조각 라우트가 자기 기본값을 따로 들고 있으면 화면과 API 가 갈린다."""
    response = client.post(
        "/ui/crawlers",
        data={"list_url": LIST_URL, "detail_url": DETAIL_URL, "default_company": "테스트"},
    )

    assert response.status_code == 200
    assert modes(conn) == ["static"]


def test_등록_화면은_가져오는_방식을_묻지_않는다() -> None:
    """운영자는 목록 URL 하나만 넣는다. 모드를 고르는 칸이 남아 있으면 그 약속이 깨진다."""
    template = (TEMPLATES / "pages" / "crawlers.html").read_text(encoding="utf-8")

    assert 'name="render_mode"' not in template
    assert "목록 URL 하나만 넣는다" in template


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
