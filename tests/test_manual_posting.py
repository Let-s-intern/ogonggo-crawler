"""공고 주소 하나로 넣는 것 테스트 (`app/crawler/manual.py`, `app/api/ui_posting_add.py`).

실사이트도 모델도 부르지 않는다. 사이트는 `httpx.MockTransport` 이고, 렌더러와 이미지 읽기와 분류는
정해 둔 답을 돌려주는 대역이다.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sqlite3
from collections.abc import Iterator, Sequence
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db, scheduler
from app.api import crawlers as crawlers_api
from app.api import ui_crawlers, ui_posting_add, workflows
from app.classify.batch import ClassifyProgress
from app.config import Settings
from app.crawler.fetcher import Fetcher, FetchResult
from app.crawler.manual import MANUAL_NAME, ManualAddError, add_posting, page_detail
from app.llm.base import ImageInput
from app.main import app

ROBOTS = "User-agent: *\nAllow: /\n"
URL = "https://careers.example.test/job_posting/rCL5Co3V"
BODY = "주요업무 · 전기 설비 견적 산출과 원가 검토를 맡습니다. " * 12

PAGE = f"""
<html><head><title>동원그룹 채용</title>
<meta property="og:title" content="[동원건설산업] 전기 견적 경력직 모집">
<meta property="og:image" content="/share.png"></head>
<body>
  <header><nav>채용공고 · 인재상 · FAQ · 로그인</nav></header>
  <main>
    <h1>[동원건설산업] 전기 견적 경력직 모집</h1>
    <p>{BODY}</p>
    <img src="/upload/posting.png"><img src="/img/logo.svg"><img src="data:image/png;base64,AAAA">
  </main>
  <footer>개인정보처리방침 · 이메일무단수집거부</footer>
</body></html>
"""
SHELL = "<html><head><title>동원그룹 채용</title></head><body><div id='root'></div></body></html>"
IMAGE_ONLY = """
<html><body><main><h1>[HD현대] 26년 하반기 신입사원 채용</h1>
<img src="https://careers.example.test/upload/notice.jpg"></main></body></html>
"""


def site(pages: dict[str, str]) -> Fetcher:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        text = pages.get(str(request.url))
        if text is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, text=text, headers={"content-type": "text/html"})

    settings = Settings(crawl_delay_seconds=0.0, crawl_max_retries=1)
    return Fetcher(settings=settings, transport=httpx.MockTransport(handle))


class FakeRenderer:
    """브라우저 대역. 렌더한 HTML 을 정해 두고 돌려준다."""

    def __init__(self, html: str) -> None:
        self.html = html
        self.fetched: list[str] = []

    async def fetch(self, url: str) -> FetchResult:
        self.fetched.append(url)
        return FetchResult(url=url, status_code=200, text=self.html)


class FakeReader:
    """이미지 읽기 대역. 받은 조각 수를 적고 정해 둔 글을 돌려준다."""

    def __init__(self) -> None:
        self.calls = 0

    async def read(self, parts: Sequence[ImageInput]) -> str:
        self.calls += 1
        return "모집분야: 설계\n지원자격: 학사 이상"


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    try:
        yield connection
    finally:
        connection.close()


def test_셀렉터_없이_페이지를_공고로_편다() -> None:
    detail = page_detail(PAGE)

    assert detail.fields["title"] == "[동원건설산업] 전기 견적 경력직 모집"
    assert "전기 설비 견적" in detail.fields["body"]
    # 메뉴와 꼬리말은 공고 글자가 아니다
    assert "로그인" not in detail.fields["body"]
    assert "개인정보처리방침" not in detail.fields["body"]
    # SVG 아이콘과 박힌 이미지는 읽을 대상이 아니다
    assert detail.images == ("/upload/posting.png",)
    assert detail.cover_image == "/share.png"


@pytest.mark.asyncio
async def test_주소_하나로_저장하고_정규화한다(conn: sqlite3.Connection) -> None:
    client = site({URL: PAGE})
    try:
        added = await add_posting(conn, URL, company="동원", fetcher=client)
    finally:
        await client.aclose()

    assert added.existed is False
    raw = conn.execute("SELECT * FROM raw_jobs WHERE id = ?", (added.raw_job_id,)).fetchone()
    assert raw["source_url"] == URL
    record = json.loads(raw["raw_data_json"])
    assert record["company_name"] == "동원"
    assert "전기 설비 견적" in record["body"]
    normalized = conn.execute(
        "SELECT title, source_url FROM normalized_jobs WHERE raw_job_id = ?", (added.raw_job_id,)
    ).fetchone()
    assert normalized["source_url"] == URL
    workflow = conn.execute(
        "SELECT name, kind, status FROM workflows WHERE id = ?", (added.workflow_id,)
    ).fetchone()
    assert tuple(workflow) == (MANUAL_NAME, "manual", "paused")


@pytest.mark.asyncio
async def test_같은_주소를_다시_넣으면_새로_저장하지_않는다(conn: sqlite3.Connection) -> None:
    client = site({URL: PAGE})
    try:
        first = await add_posting(conn, URL, fetcher=client)
        second = await add_posting(conn, URL, fetcher=client)
    finally:
        await client.aclose()

    assert second.existed is True
    assert second.raw_job_id == first.raw_job_id
    assert conn.execute("SELECT count(*) FROM raw_jobs").fetchone()[0] == 1
    # 직접 추가 워크플로우는 하나뿐이다
    assert conn.execute("SELECT count(*) FROM workflows WHERE kind = 'manual'").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_정적_HTML_이_껍데기면_브라우저로_연다(conn: sqlite3.Connection) -> None:
    renderer = FakeRenderer(PAGE)

    async def open_renderer() -> Any:
        return renderer

    client = site({URL: SHELL})
    try:
        added = await add_posting(conn, URL, fetcher=client, open_renderer=open_renderer)
    finally:
        await client.aclose()

    assert renderer.fetched == [URL]
    assert any("브라우저로 열었다" in note for note in added.notes)
    raw = conn.execute("SELECT raw_data_json FROM raw_jobs").fetchone()
    assert "전기 설비 견적" in json.loads(raw["raw_data_json"])["body"]


@pytest.mark.asyncio
async def test_본문이_이미지뿐이면_이미지를_읽는다(conn: sqlite3.Connection) -> None:
    """HD현대 공고는 이미지 한 장이 본문이다. 읽지 않으면 분류할 글이 없다."""
    image = httpx.Response(200, content=_png(), headers={"content-type": "image/png"})
    reader = FakeReader()

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        if request.url.path.endswith(".jpg"):
            return image
        return httpx.Response(200, text=IMAGE_ONLY, headers={"content-type": "text/html"})

    client = Fetcher(
        settings=Settings(crawl_delay_seconds=0.0, crawl_max_retries=1),
        transport=httpx.MockTransport(handle),
    )
    try:
        await add_posting(conn, URL, fetcher=client, reader=reader)
    finally:
        await client.aclose()

    assert reader.calls == 1
    record = json.loads(conn.execute("SELECT raw_data_json FROM raw_jobs").fetchone()[0])
    assert "지원자격: 학사 이상" in record["body"]


@pytest.mark.asyncio
async def test_http_주소가_아니면_넣지_않는다(conn: sqlite3.Connection) -> None:
    client = site({})
    try:
        with pytest.raises(ManualAddError):
            await add_posting(conn, "careers.example.test/jobs/1", fetcher=client)
    finally:
        await client.aclose()

    assert conn.execute("SELECT count(*) FROM raw_jobs").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_직접_추가_워크플로우는_사이트로_보이지_않는다(conn: sqlite3.Connection) -> None:
    """돌 목록이 없는 자리다. 사이트 목록·크롤러 목록에 섞이면 켜고 끌 수 있는 사이트로 보인다."""
    client = site({URL: PAGE})
    try:
        added = await add_posting(conn, URL, fetcher=client)
    finally:
        await client.aclose()
    # 멈춘 채로 만들지만, 누가 켜도 스케줄러는 돌리지 않는다
    conn.execute("UPDATE workflows SET status = 'active' WHERE id = ?", (added.workflow_id,))

    assert workflows.list_workflows(conn) == []
    assert added.workflow_id not in scheduler._active_workflows(conn)
    assert conn.execute(ui_crawlers._LIST_QUERY).fetchall() == []


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


def test_화면에서_넣으면_저장하고_분류를_뒤에_건다(
    client: TestClient,
    conn: sqlite3.Connection,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """저장까지만 창에서 기다린다. 큰 페이지는 분류에 십수 분이 걸려 요청이 끊긴다."""
    fake = site({URL: PAGE})
    launched: list[Any] = []
    classified: list[list[int]] = []

    async def classify(
        connection: sqlite3.Connection, ids: list[int], progress: ClassifyProgress, **_: Any
    ) -> ClassifyProgress:
        classified.append(ids)
        connection.execute(
            "UPDATE normalized_jobs SET job_field = 'IT·개발', job_role = '기타IT·개발'"
            " WHERE raw_job_id = ?",
            (ids[0],),
        )
        progress.processed = 1
        return progress

    monkeypatch.setattr(ui_posting_add, "get_fetcher", lambda: fake)
    monkeypatch.setattr(ui_posting_add, "classify_ids", classify)
    app.dependency_overrides[ui_posting_add.get_add_launcher] = lambda: launched.append
    app.dependency_overrides[ui_posting_add.get_add_connect] = lambda: (
        lambda: db.connect(tmp_path / "jobs.db")
    )

    html = client.post("/ui/postings/new", data={"url": URL, "company": "동원"}).text

    assert "저장했어요" in html
    assert "AI 분류는 뒤에서 돌고 있어요" in html
    assert "/review?workflow_id=" in html
    # 응답이 나간 뒤에 분류가 돈다
    assert classified == []
    assert len(launched) == 1
    asyncio.run(launched[0])
    assert len(classified) == 1
    row = conn.execute("SELECT job_field, job_role FROM normalized_jobs").fetchone()
    assert tuple(row) == ("IT·개발", "기타IT·개발")


def test_사이트_화면에_공고_한_건_추가_버튼이_있다(client: TestClient) -> None:
    html = client.get("/workflows").text

    assert 'hx-get="/ui/postings/new"' in html
    assert "공고 한 건 추가" in html


def _png() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (10, 10), "white").save(buffer, format="PNG")
    return buffer.getvalue()
