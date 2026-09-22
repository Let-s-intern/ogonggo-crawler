"""상세 URL 없이 등록하기.

운영자는 목록 URL 하나만 넣는다. 상세로 가는 길은 판정이 찾고, **찾았으면 그 페이지를 보고
상세 셀렉터까지 만든다** — 그래야 목록 URL 하나로 등록이 끝난다.

찾지 못하는 사이트도 있다. 상세를 JS 로 그려 공고마다 주소가 따로 없으면 없는 주소를
지어내지 않고, 상세 셀렉터는 어느 HTML 에도 돌려보지 않은 채로 남는다. 그 상태는 실패가
아니라 건너뜀이다 — 0개 매칭을 실패로 적으면 운영자는 고칠 곳으로 읽는데, 여기서는 볼
페이지가 없었을 뿐이다.

Gemini 도 실사이트도 부르지 않는다. 생성 의존성과 fetch 클라이언트를 갈아끼운다.
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
from app.crawler.fetcher import FetchResult
from app.main import app
from app.selector.detail_path import document_path
from app.selector.discovery import Discovery
from app.selector.generator import GenerationResult, Usage
from app.selector.schema import SelectorSet, validate_selectors
from app.selector.verify import FAILED, OK, SKIPPED, FieldMatch, VerificationReport

LIST_URL = "https://www.samsungcareers.com/hr/"
DETAIL_URL = "https://www.samsungcareers.com/hr/1"

SELECTORS: dict[str, Any] = {
    "list": {
        "item": "ul.list > li",
        "title": "a.tit",
        "link": "a.tit",
        "date": "span.date",
    },
    "detail": {
        "title": "h1",
        "body": "div.cont",
        "qualifications": "",
        "recruitment_end_at": "",
        "department": "",
    },
}

# 모델이 "사이트에 없다" 고 답해 셀렉터가 비어 있는 필드. 매칭 0개지만 실패가 아니다
ABSENT = frozenset(
    {
        "list.company_name",
        "detail.qualifications",
        "detail.recruitment_end_at",
        "detail.department",
        "detail.company_name",
    }
)

# 상세 필드는 판정되지 않는다. 목록 필드만 실제 HTML 에 돌아간 결과다
MATCHES = {
    "list.item": 12,
    "list.title": 12,
    "list.link": 12,
    "list.date": 12,
    "list.company_name": 0,
    "detail.title": 0,
    "detail.body": 0,
    "detail.qualifications": 0,
    "detail.recruitment_end_at": 0,
    "detail.department": 0,
    "detail.company_name": 0,
}


def Verified() -> VerificationReport:  # noqa: N802
    """검증 결과. 상세 HTML 이 비어 있어 상세 필드가 0개로 온 모양이다.

    진짜 `VerificationReport` 를 만든다. 등록이 목록 판정과 상세 판정을 갈라 합치므로
    (`app/api/crawlers.py` 의 `_fill_detail`) 대역이 필드 목록을 들고 있어야 한다.
    """
    return VerificationReport(
        fields=[
            FieldMatch(
                name=name,
                selector="" if name in ABSENT else "x",
                matches=count,
                # 셀렉터가 비어 있는 선택 필드는 건너뜀이다. 매칭 0개지만 고칠 것이 없다
                status=SKIPPED if name in ABSENT else (OK if count else FAILED),
            )
            for name, count in MATCHES.items()
        ]
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


@pytest.fixture
def called_with() -> list[tuple[str, str]]:
    """생성이 어떤 (리스트 URL, 상세 URL) 로 불렸는지 쌓인다."""
    calls: list[tuple[str, str]] = []

    async def generate(list_url: str, detail_url: str, render_mode: str) -> GenerationResult:
        calls.append((list_url, detail_url))
        return GenerationResult(
            selectors=validate_selectors(SELECTORS),
            usage=Usage(
                provider="gemini",
                model="gemini-3.5-flash",
                input_tokens=8000,
                output_tokens=120,
                total_tokens=8120,
                latency_ms=5000,
            ),
            attempts=1,
            verification=Verified(),
        )

    app.dependency_overrides[crawlers_api.get_generator] = lambda: generate
    stub_discoverer()
    return calls


def saved(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM crawlers").fetchone()
    assert row is not None
    return row


def test_상세_URL_없이_등록되고_판정이_찾은_페이지로_상세를_만든다(
    client: TestClient, conn: sqlite3.Connection, called_with: list[tuple[str, str]]
) -> None:
    """빈 값이 거절 사유가 되지 않는다. 판정이 찾은 상세 주소가 그다음 생성의 입력이 된다."""
    response = client.post("/api/crawlers", json={"list_url": LIST_URL})

    assert response.status_code == 201
    # 첫 생성은 목록만 보고, 두 번째는 판정이 알아낸 상세 페이지를 본다
    assert called_with == [(LIST_URL, ""), (LIST_URL, f"{LIST_URL}1/")]
    assert response.json()["detail_url"] == f"{LIST_URL}1/"
    assert saved(conn)["detail_url"] == f"{LIST_URL}1/"


def test_공백만_적은_상세_URL_은_안_적은_것이다(
    client: TestClient, conn: sqlite3.Connection, called_with: list[tuple[str, str]]
) -> None:
    response = client.post("/api/crawlers", json={"list_url": LIST_URL, "detail_url": "   "})

    assert response.status_code == 201
    # 안 적은 것과 같다. 판정이 찾은 주소가 저장된다
    assert saved(conn)["detail_url"] == f"{LIST_URL}1/"


def test_상세_페이지를_못_찾으면_실패가_아니라_건너뜀이다(
    client: TestClient, conn: sqlite3.Connection, called_with: list[tuple[str, str]]
) -> None:
    """볼 페이지가 없어 판정하지 않은 것이다. 고칠 곳 목록에 섞지 않는다."""
    stub_discoverer(found=False)
    body = client.post("/api/crawlers", json={"list_url": LIST_URL}).json()

    assert body["failed_fields"] == []
    assert "detail.title" in body["skipped_fields"]
    assert "detail.body" in body["skipped_fields"]
    # 잡힌 목록 필드는 건너뜀이 아니다. 셀렉터가 비어 있는 `list.company_name` 만 그 자리다
    assert [name for name in body["skipped_fields"] if name.startswith("list.")] == [
        "list.company_name"
    ]
    assert any("상세 URL 이 없어" in note for note in body["notes"])


def test_상세_URL_을_주면_건너뛰지_않는다(
    client: TestClient, conn: sqlite3.Connection, called_with: list[tuple[str, str]]
) -> None:
    """볼 페이지가 있었으면 0개 매칭은 그대로 실패다."""
    body = client.post(
        "/api/crawlers", json={"list_url": LIST_URL, "detail_url": DETAIL_URL}
    ).json()

    # 볼 페이지가 있었으므로 상세 필드는 판정됐다. 건너뜀으로 남는 것은 셀렉터가 비어 있는
    # 선택 필드뿐이다
    assert "detail.title" not in body["skipped_fields"]
    assert body["failed_fields"] == ["detail.title", "detail.body"]
    assert saved(conn)["detail_url"] == DETAIL_URL


def test_화면_결과에_건너뜀_이_단어로_나온다(
    client: TestClient, conn: sqlite3.Connection, called_with: list[tuple[str, str]]
) -> None:
    """실패로 적으면 운영자는 고칠 곳으로 읽는다 (`.claude/rules/writing.md`)."""
    stub_discoverer(found=False)
    html = client.post(
        "/ui/crawlers", data={"list_url": LIST_URL, "default_company": "테스트"}
    ).text

    assert "건너뜀" in html
    # 12.4 에서 문구가 "확인하지 않은 필드" 에서 바뀌었다. 셀렉터가 비어 건너뛴 것까지
    # 같은 줄에 들어오면서 상세 URL 만 가리키던 이름이 맞지 않게 됐다
    assert "건너뛴 필드" in html
    assert "손으로 고쳐야 하는 필드" not in html


async def test_상세_URL_이_없으면_목록만_가져온다(monkeypatch: pytest.MonkeyPatch) -> None:
    """없는 주소를 지어내 가져오지 않는다. 기본 생성 경로가 그렇게 되어 있는지 본다."""
    fetched: list[str] = []
    generated: list[tuple[str, str]] = []

    class StubSource:
        async def fetch(self, url: str) -> FetchResult:
            fetched.append(url)
            return FetchResult(url=url, status_code=200, text="<ul class='list'></ul>")

    async def stub_generate_from_html(
        list_html: str, detail_html: str, **kwargs: Any
    ) -> GenerationResult:
        generated.append((list_html, detail_html))
        return GenerationResult(
            selectors=validate_selectors(SELECTORS),
            usage=Usage(
                provider="gemini",
                model="gemini-3.5-flash",
                input_tokens=1,
                output_tokens=1,
                total_tokens=2,
                latency_ms=1,
            ),
            attempts=1,
            verification=Verified(),
        )

    async def unreachable(*args: Any, **kwargs: Any) -> GenerationResult:
        raise AssertionError("상세 URL 이 없으면 두 페이지 경로로 가지 않는다")

    monkeypatch.setattr(crawlers_api, "get_fetcher", StubSource)
    monkeypatch.setattr(crawlers_api, "generate_from_html", stub_generate_from_html)
    monkeypatch.setattr(crawlers_api, "generate_for_urls", unreachable)

    generate = crawlers_api.get_generator(None)
    result = await generate(LIST_URL, "", "static")

    assert result.selectors.list.item == "ul.list > li"
    assert fetched == [LIST_URL]
    # 상세 HTML 은 빈 문자열로 들어간다. 없는 페이지를 대신할 것을 지어내지 않는다
    assert generated == [("<ul class='list'></ul>", "")]


def stub_discoverer(found: bool = True) -> None:
    """경로 판정도 갈아끼운다. 기본 경로는 실사이트를 다시 가져오고 브라우저까지 연다.

    등록은 셀렉터 생성 다음에 상세로 가는 길을 알아본다 (`app/api/crawlers.py` 의
    `create_crawler`). 여기서 갈아끼우지 않으면 이 테스트가 네트워크에 매달린다
    (`.claude/rules/core.md`).

    `found` 가 거짓이면 상세로 가는 길을 못 찾은 사이트다. 그때는 볼 페이지가 없어 상세
    셀렉터가 판정되지 않는다.
    """

    async def discover(list_url: str, selectors: SelectorSet) -> Discovery:
        if not found:
            return Discovery(
                list_mode="static",
                evidence="렌더 후 항목은 잡았지만 상세로 가는 길이 없다",
                failure="detail_unreachable",
                reason="항목에 상세 주소가 없고 클릭해도 아무 일이 없다",
                list_count=1,
            )
        return Discovery(
            list_mode="static",
            detail_mode="static",
            detail=document_path(f"{list_url}1/", "목록 항목의 링크를 그대로 따라간다"),
            evidence="정적 목록에서 항목과 상세 주소를 찾았다. 브라우저를 띄우지 않았다",
            list_count=1,
        )

    app.dependency_overrides[crawlers_api.get_discoverer] = lambda: discover


def test_공고_한_건만_열었으면_실패여도_그_페이지로_상세를_만든다(
    client: TestClient, conn: sqlite3.Connection, called_with: list[tuple[str, str]]
) -> None:
    """HD현대·동원. 나머지 공고로 갈 길만 없었다. 목록 경로를 손으로 채우면 바로 돌게 둔다."""
    opened = f"{LIST_URL}job_posting/rCL5Co3V"

    async def discover(list_url: str, selectors: SelectorSet) -> Discovery:
        return Discovery(
            list_mode="playwright",
            detail_mode="static",
            detail=document_path(opened, "그 주소는 정적으로도 열렸다"),
            evidence="렌더 후 10건, 항목에 상세 주소가 없어 클릭했다",
            failure="detail_unreachable",
            reason="공고 한 건을 눌러 상세는 열었지만, 공고마다 다른 상세 주소를 만들 재료가 없다",
            list_count=10,
        )

    app.dependency_overrides[crawlers_api.get_discoverer] = lambda: discover
    body = client.post("/api/crawlers", json={"list_url": LIST_URL}).json()

    assert called_with == [(LIST_URL, ""), (LIST_URL, opened)]
    assert body["path_failure"] == "detail_unreachable"
    row = saved(conn)
    assert row["detail_url"] == opened
    # 열어 본 결과대로다. 판정이 실패했다고 셀렉터를 만든 경로로 되돌리지 않는다
    assert row["detail_mode"] == "static"


def test_판정이_AI_에게_물었으면_그_비용을_남긴다(
    client: TestClient, conn: sqlite3.Connection, called_with: list[tuple[str, str]]
) -> None:
    """버린 제안에도 돈은 나갔다. 셀렉터 생성과 같은 칸에 센다 — 등록의 한 부분이다."""
    spent = Usage(
        provider="deepseek",
        model="deepseek-flash",
        input_tokens=2400,
        output_tokens=650,
        total_tokens=3050,
        latency_ms=2900,
    )

    async def discover(list_url: str, selectors: SelectorSet) -> Discovery:
        return Discovery(
            list_mode="api",
            detail_mode="static",
            detail=document_path(f"{list_url}1/", "그 주소는 정적으로도 열렸다"),
            evidence="AI 제안을 확인해 채택했다",
            list_count=10,
            ai_usage=spent,
        )

    app.dependency_overrides[crawlers_api.get_discoverer] = lambda: discover
    client.post("/api/crawlers", json={"list_url": LIST_URL})

    rows = conn.execute(
        "SELECT feature, model, total_tokens FROM llm_calls WHERE model = 'deepseek-flash'"
    ).fetchall()
    assert [tuple(row) for row in rows] == [("selector_generate", "deepseek-flash", 3050)]


@pytest.mark.parametrize("holds", [True, False])
def test_목록이_렌더면_상세_셀렉터도_렌더한_페이지로_만든다(
    client: TestClient,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    holds: bool,
) -> None:
    """슈피겐 실측(2026-09-22): 상세만 정적이라 정적으로 다시 받자 JS 목록이 비어, 모델이 상세
    칸까지 비워 답했다. 어느 한쪽이라도 렌더가 필요하면 렌더로 다시 받는다."""
    modes: list[str] = []

    async def generate(list_url: str, detail_url: str, render_mode: str) -> GenerationResult:
        modes.append(render_mode)
        return GenerationResult(
            selectors=validate_selectors(SELECTORS),
            usage=Usage(
                provider="deepseek",
                model="deepseek-flash",
                input_tokens=1,
                output_tokens=1,
                total_tokens=2,
                latency_ms=1,
            ),
            attempts=1,
            verification=Verified(),
            render_mode="playwright",
        )

    async def discover(list_url: str, selectors: SelectorSet) -> Discovery:
        return Discovery(
            list_mode="api",
            detail_mode="static",
            detail=document_path(f"{LIST_URL}job_posting/s44YbZq6", "정적으로도 열렸다"),
            evidence="목록 API 를 채택했다",
            list_count=8,
        )

    async def static_holds(url: str, selectors: SelectorSet) -> bool:
        return holds

    monkeypatch.setattr(crawlers_api, "_static_detail_holds", static_holds)
    app.dependency_overrides[crawlers_api.get_generator] = lambda: generate
    app.dependency_overrides[crawlers_api.get_discoverer] = lambda: discover
    client.post("/api/crawlers", json={"list_url": LIST_URL})

    assert modes == ["", "playwright"]
    # 렌더한 페이지로 만든 상세 셀렉터가 정적 응답에서 제목·본문을 못 잡으면 상세도 렌더로 둔다
    # (한독, 2026-09-22)
    assert saved(conn)["detail_mode"] == ("static" if holds else "playwright")
