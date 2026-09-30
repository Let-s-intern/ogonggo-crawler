"""사이트 화면 — 한 줄 목록과 오른쪽 패널 (2026-09-17, LC-3344).

실사이트에 나가지 않는다. 셀렉터 생성은 가짜 생성기로 바꿔 끼운다.

| 확인 | 깨지면 |
|---|---|
| 사이트마다 한 줄이고 문제 있는 사이트가 위로 온다 | 고칠 사이트를 찾아 목록을 뒤진다 |
| 실패 사유를 쉬운 말로 적고 `고치기` 를 붙인다 | `detail_unreachable` 같은 코드만 보인다 |
| 패널에 조작(카드)·고치기·최근 수집이 있다 | 목록에서 할 수 있던 일을 잃는다 |
| 공고 주소로 셀렉터를 다시 만들어 저장 없이 편집기에 올린다 | 확인 없이 셀렉터가 바뀐다 |
| 패널에서 올린 로고가 그 사이트 회사의 로고가 된다 | 로고를 바꾸려고 회사 화면을 뒤진다 |
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi.testclient import TestClient

from app import companies, db
from app.api import crawlers as crawlers_api
from app.api import workflows as workflows_api
from app.main import app
from app.normalize.engine import insert_normalized
from app.scheduler import WorkflowScheduler
from app.storage import s3
from app.storage import settings as store

LIST_URL = "https://careers.example.com/jobs"


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    for number, name, status in (
        (1, "잘 되는 곳", "active"),
        (2, "고장 난 곳", "active"),
        (3, "멈춘 곳", "paused"),
    ):
        connection.execute(
            "INSERT INTO crawlers (id, name, list_url, status, selectors_json)"
            " VALUES (?, ?, ?, 'promoted', '{}')",
            (number, name, f"{LIST_URL}/{number}"),
        )
        connection.execute(
            "INSERT INTO workflows (id, crawler_id, name, status) VALUES (?, ?, ?, ?)",
            (number, number, name, status),
        )
    connection.execute(
        "INSERT INTO crawl_runs (workflow_id, status, new_count) VALUES (1, 'success', 4)"
    )
    connection.execute(
        "INSERT INTO crawl_runs (workflow_id, status, fail_count, error_class, error_message)"
        " VALUES (2, 'failed', 9, 'detail_unreachable', '상세로 갈 길이 없다')"
    )
    connection.execute("UPDATE workflows SET last_run_at = datetime('now') WHERE id IN (1, 2)")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def scheduler() -> Iterator[WorkflowScheduler]:
    async def do_nothing(workflow_id: int) -> None:
        return None

    instance = WorkflowScheduler(scheduler=AsyncIOScheduler(timezone="UTC"), runner=do_nothing)
    try:
        yield instance
    finally:
        instance.shutdown()


@pytest.fixture
def client(
    tmp_path: pathlib.Path, conn: sqlite3.Connection, scheduler: WorkflowScheduler
) -> Iterator[TestClient]:
    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(tmp_path / "jobs.db")
        try:
            yield connection
        finally:
            connection.close()

    app.dependency_overrides[workflows_api.get_connection] = request_connection
    app.dependency_overrides[crawlers_api.get_connection] = request_connection
    app.dependency_overrides[workflows_api.get_workflow_scheduler] = lambda: scheduler
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_사이트_화면이_목록_조각을_부른다(client: TestClient) -> None:
    body = client.get("/workflows").text

    assert 'hx-get="/ui/sites"' in body
    assert "+ 사이트 추가" in body


def test_사이트마다_한_줄이고_문제_있는_곳이_위로_온다(client: TestClient) -> None:
    html = client.get("/ui/sites").text

    assert html.count('class="site-row"') == 3
    assert html.index("고장 난 곳") < html.index("잘 되는 곳") < html.index("멈춘 곳")


def test_실패는_쉬운_말로_적고_고치기를_붙인다(client: TestClient) -> None:
    html = client.get("/ui/sites").text
    broken = html[html.index("고장 난 곳") : html.index("잘 되는 곳")]

    assert "상세 페이지로 들어가지 못했어요" in broken
    assert "고치기" in broken
    assert "detail_unreachable" not in broken
    assert 'hx-get="/ui/sites/2/panel"' in html


def test_패널에_문제_조작_고치기_최근_수집이_있다(client: TestClient) -> None:
    html = client.get("/ui/sites/2/panel").text

    assert "무엇이 문제인가요" in html
    assert 'id="workflow-row-2"' in html  # 조작은 워크플로우 카드 그대로다
    assert 'hx-post="/ui/crawlers/2/test-run"' in html
    assert 'hx-post="/ui/sites/2/detail-url"' in html
    assert 'id="test-result"' in html and 'id="test-repair"' in html
    assert "최근 수집" in html


def test_잘_되는_사이트에는_문제_상자가_없다(client: TestClient) -> None:
    html = client.get("/ui/sites/1/panel").text

    assert "무엇이 문제인가요" not in html
    assert "고칠 필요가 없습니다" in html


def test_없는_사이트는_사유를_적는다(client: TestClient) -> None:
    assert "사이트를 찾지 못했다" in client.get("/ui/sites/99/panel").text


def test_공고_주소는_http_로_시작해야_한다(client: TestClient) -> None:
    html = client.post("/ui/sites/2/detail-url", data={"detail_url": "not a url"}).text

    assert "http:// 나 https:// 로 시작해야 한다" in html


def test_공고_주소로_만든_셀렉터는_저장하지_않고_편집기에_올린다(
    client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.selector.generator import GenerationResult
    from app.selector.schema import validate_selectors
    from app.selector.verify import VerificationReport

    asked: list[tuple[str, str, str]] = []
    selectors = validate_selectors(
        {
            "list": {"item": "li.job", "title": "a", "link": "a", "date": ""},
            "detail": {
                "title": "h1.new-title",
                "body": "div.new-body",
                "qualifications": "",
                "recruitment_end_at": "",
                "department": "",
            },
        }
    )

    def fake_generator(_conn: sqlite3.Connection) -> object:
        async def generate(list_url: str, detail_url: str, render_mode: str) -> GenerationResult:
            asked.append((list_url, detail_url, render_mode))
            return GenerationResult(
                selectors=selectors,
                usage=None,  # type: ignore[arg-type]
                attempts=1,
                verification=VerificationReport(fields=[]),
            )

        return generate

    monkeypatch.setattr(crawlers_api, "get_generator", fake_generator)
    html = client.post(
        "/ui/sites/2/detail-url", data={"detail_url": "https://careers.example.com/jobs/10"}
    ).text

    # 정적 사이트도 모드를 비워 넘긴다. 등록처럼 정적이 안 되면 렌더로 만들어 본다
    assert asked == [(f"{LIST_URL}/2", "https://careers.example.com/jobs/10", "")]
    assert "아직 저장하지 않았다" in html
    assert "h1.new-title" in html
    saved = conn.execute("SELECT selectors_json FROM crawlers WHERE id = 2").fetchone()
    assert saved["selectors_json"] == "{}"
    # 넣은 주소는 예시 공고 주소로 남는다. 다음에 패널을 열면 보인다
    detail = conn.execute("SELECT detail_url FROM crawlers WHERE id = 2").fetchone()
    assert detail["detail_url"] == "https://careers.example.com/jobs/10"


def test_패널은_저장된_목록_주소와_예시_공고_주소를_채워_보인다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    conn.execute(
        "UPDATE crawlers SET detail_url = 'https://careers.example.com/jobs/7' WHERE id = 2"
    )
    conn.commit()

    html = client.get("/ui/sites/2/panel").text

    assert "예시 공고 주소" in html
    assert "공고 하나의 주소" not in html
    assert f'value="{LIST_URL}/2"' in html
    assert 'value="https://careers.example.com/jobs/7"' in html
    assert 'hx-post="/ui/sites/2/urls"' in html


def test_목록_주소와_예시_공고_주소를_고쳐_저장한다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    html = client.post(
        "/ui/sites/2/urls",
        data={
            "list_url": "https://careers.example.com/new-list",
            "detail_url": "https://careers.example.com/jobs/8",
        },
    ).text

    assert "주소를 저장했어요" in html
    assert 'value="https://careers.example.com/new-list"' in html
    row = conn.execute(
        "SELECT list_url, detail_url, selectors_json FROM crawlers WHERE id = 2"
    ).fetchone()
    assert row["list_url"] == "https://careers.example.com/new-list"
    assert row["detail_url"] == "https://careers.example.com/jobs/8"
    assert row["selectors_json"] == "{}"  # 셀렉터는 건드리지 않는다


def test_예시_공고_주소는_비워_저장할_수_있다(client: TestClient, conn: sqlite3.Connection) -> None:
    conn.execute(
        "UPDATE crawlers SET detail_url = 'https://careers.example.com/jobs/7' WHERE id = 2"
    )
    conn.commit()

    client.post("/ui/sites/2/urls", data={"list_url": f"{LIST_URL}/2", "detail_url": ""})

    assert conn.execute("SELECT detail_url FROM crawlers WHERE id = 2").fetchone()[0] is None


def test_주소가_http_가_아니면_저장하지_않는다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    html = client.post("/ui/sites/2/urls", data={"list_url": "not a url", "detail_url": ""}).text

    assert "목록 주소는 http://" in html
    assert (
        conn.execute("SELECT list_url FROM crawlers WHERE id = 2").fetchone()[0] == f"{LIST_URL}/2"
    )


def fill_storage(conn: sqlite3.Connection) -> None:
    store.write_config(
        conn,
        store.StorageConfig(
            endpoint="http://minio:9000",
            region="us-east-1",
            bucket="logos",
            access_key="minioadmin",
            secret_key="minioadmin",
            public_base="http://localhost:9000/logos",
        ),
    )
    conn.commit()


def test_그룹_채용_사이트로_바꾸면_이미_모은_공고의_회사명도_바뀐다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    conn.execute("UPDATE crawlers SET default_company = '삼성' WHERE id = 1")
    raw = {
        "source_url": "https://x/1",
        "title": "백엔드",
        "body": "본문",
        "company_name": "삼성SDS",
    }
    conn.execute(
        "INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)"
        " VALUES (1, 1, 'https://x/1', ?, 'hash')",
        (json.dumps(raw, ensure_ascii=False),),
    )
    insert_normalized(conn, 1, [])
    conn.commit()

    def company() -> tuple[str | None, str | None]:
        row = conn.execute(
            "SELECT company_name, parent_company_name FROM normalized_jobs"
        ).fetchone()
        return row["company_name"], row["parent_company_name"]

    # 기본은 회사가 하나인 사이트다. 공고에서 읽은 이름은 쓰지 않는다
    assert company() == (None, "삼성")
    panel = client.get("/ui/sites/1/panel").text
    assert 'hx-post="/ui/sites/1/affiliates"' in panel
    assert 'name="has_affiliates" value="1" class="mt-0">' in panel

    body = client.post("/ui/sites/1/affiliates", data={"has_affiliates": "1"}).text

    assert "그룹 채용 사이트로 바꿨어요" in body and "1건" in body
    assert 'name="has_affiliates" value="1" class="mt-0" checked>' in body
    assert company() == ("삼성SDS", "삼성")

    # 체크를 풀면 칸이 오지 않는다
    body = client.post("/ui/sites/1/affiliates", data={}).text

    assert "회사가 하나인 사이트로 바꿨어요" in body
    assert company() == (None, "삼성")


def test_패널에서_올린_로고가_그_사이트_회사의_로고가_된다(
    client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn.execute("UPDATE crawlers SET default_company = '예시' WHERE id = 1")
    conn.commit()
    fill_storage(conn)
    uploaded: list[bytes] = []

    def upload(config: store.StorageConfig, *, data: bytes, name: str) -> str:
        uploaded.append(data)
        return f"http://localhost:9000/logos/{name}.png"

    monkeypatch.setattr(s3, "upload_image", upload)

    panel = client.get("/ui/sites/1/panel").text
    assert 'hx-post="/ui/sites/1/logo"' in panel
    assert 'name="logo" type="file"' in panel

    body = client.post(
        "/ui/sites/1/logo", files={"logo": ("logo.png", b"PNG-logo", "image/png")}
    ).text

    assert uploaded == [b"PNG-logo"]
    company = companies.read(conn, "예시")
    assert company is not None and (company.logo_url or "").startswith("http://localhost:9000/")
    assert "로고를 바꿨어요" in body
    assert f'src="{company.logo_url}"' in body


def test_회사_이름이_없거나_저장소가_비면_올리지_않는다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    body = client.post(
        "/ui/sites/1/logo", files={"logo": ("logo.png", b"PNG-logo", "image/png")}
    ).text
    assert "회사 이름이 없어" in body

    conn.execute("UPDATE crawlers SET default_company = '예시' WHERE id = 1")
    conn.commit()
    panel = client.get("/ui/sites/1/panel").text
    assert 'name="logo"' not in panel
    assert "파일 저장소" in panel

    body = client.post(
        "/ui/sites/1/logo", files={"logo": ("logo.png", b"not an image", "image/png")}
    ).text
    assert "로고를 올리지 못했어요" in body
    assert companies.read(conn, "예시") is None


def test_정적_HTML_에_목록이_없어_렌더로_만들면_저장할_때_수집_모드도_바뀐다(
    client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-09-30 한솔. 목록을 JS 로 그리게 바뀐 사이트는 정적으로 묶인 다시 찾기로 못 찾았다."""
    from app.selector.generator import GenerationResult
    from app.selector.schema import validate_selectors
    from app.selector.verify import VerificationReport

    selectors = validate_selectors(
        {
            "list": {"item": "li.job", "title": "a", "link": "a", "date": ""},
            "detail": {
                "title": "h1",
                "body": "div.body",
                "qualifications": "",
                "recruitment_end_at": "",
                "department": "",
            },
        }
    )

    def fake_generator(_conn: sqlite3.Connection) -> object:
        async def generate(list_url: str, detail_url: str, render_mode: str) -> GenerationResult:
            return GenerationResult(
                selectors=selectors,
                usage=None,  # type: ignore[arg-type]
                attempts=1,
                verification=VerificationReport(fields=[]),
                render_mode="playwright",
            )

        return generate

    monkeypatch.setattr(crawlers_api, "get_generator", fake_generator)
    html = client.post(
        "/ui/sites/2/detail-url", data={"detail_url": "https://careers.example.com/jobs/10"}
    ).text

    assert "렌더 HTML 로 만들었다" in html
    assert 'name="render_mode" value="playwright"' in html
    mode = conn.execute("SELECT list_mode FROM crawlers WHERE id = 2").fetchone()
    assert mode["list_mode"] == "static"

    saved = client.put(
        "/ui/tests/2/selectors",
        data={"selectors_json": selectors.model_dump_json(), "render_mode": "playwright"},
    ).text

    assert "수집 모드도 렌더로 바꿨다" in saved
    modes = conn.execute("SELECT list_mode, detail_mode FROM crawlers WHERE id = 2").fetchone()
    assert (modes["list_mode"], modes["detail_mode"]) == ("playwright", "playwright")
