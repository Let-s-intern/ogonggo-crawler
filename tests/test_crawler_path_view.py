"""크롤러가 어떤 방식으로 도는지를 화면에 낱말로 적는다 (5.1).

`list_mode`/`detail_mode`/`api_config_json` 은 저장값이라 그대로는 읽히지 않는다. 화면에는
목록을 얻는 법과 상세로 가는 법을 낱말로 적고, 낱말 옆에 실제 엔드포인트나 주소 형식을 남긴다.

| 확인 | 깨지면 |
|---|---|
| 여섯 크롤러의 경로가 낱말로 나온다 | 저장값만 보고 어느 사이트가 API 로 도는지 알 수 없다 |
| 낱말 옆에 엔드포인트가 붙는다 | 같은 낱말을 단 두 크롤러가 다른 곳을 부르는 것이 안 보인다 |
| 마지막 확인 시각이 `as_time` 을 지난다 | 화면이 UTC 를 그린다 |
| 운영자가 목록·상세를 따로 정한다 | `api` 로 도는 크롤러가 정적으로 조용히 내려앉는다 |
| 저장한 경로를 자동 판정이 덮어쓰지 않는다 | 고쳐 둔 경로가 다음 등록 판정에 지워진다 |
| 등록 결과에 근거 문장이 나온다 | 왜 그 경로로 정해졌는지 다음 사람이 처음부터 다시 잰다 |
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
from app.api import ui
from app.config import get_settings
from app.main import app
from app.selector.detail_path import DetailPath, document_path
from app.selector.discovery import Discovery
from app.selector.generator import GenerationResult, Usage
from app.selector.schema import SelectorSet, validate_selectors
from app.selector.verify import verify_selectors

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
LIST_HTML = (FIXTURES / "pythonorg-jobs-list-20260821.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "pythonorg-job-detail-20260821.html").read_text(encoding="utf-8")
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

# 실제로 저장돼 있는 모양이다 (LG). 목록도 상세도 API 로 돈다
LG_CONFIG = {
    "list": {
        "url": "https://api.careers.lg.com/rmk/job/retrieveJobNoticesList",
        "method": "POST",
        "body": {"order": "DESC"},
        "items_path": "data.jobNoticeList",
        "fields": {"title": "jobNoticeName", "date": "recEndDateTime"},
        "id_field": "jobNoticeId",
        "link_template": "https://careers.lg.com/app/jobs/detail/{id}",
    },
    "detail": {
        "url": "https://api.careers.lg.com/rmk/job/retrieveJobNoticesDetail",
        "method": "POST",
        "body": {"jobNoticeId": "{id}"},
        "fields": {"title": "data.jobNoticeName", "body": "data.recList.*.detailContext"},
    },
}

# 목록은 API 인데 상세는 서버가 그려 주는 문서다 (SK)
SK_CONFIG = {
    "list": {
        "url": "https://www.skcareers.com/Recruit/GetRecruitList",
        "method": "POST",
        "body": {"sort": "2"},
        "items_path": "list",
        "fields": {"title": "title", "date": "end"},
        "id_field": "noticeID",
        "link_template": "https://www.skcareers.com/Recruit/Detail/{id}",
    }
}


def add_crawler(
    conn: sqlite3.Connection,
    name: str,
    *,
    list_mode: str,
    detail_mode: str,
    api_config: dict[str, Any] | None = None,
    selectors: dict[str, Any] | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO crawlers (name, list_url, selectors_json, list_mode, detail_mode,
                              api_config_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            name,
            "https://example.test/jobs",
            json.dumps(selectors) if selectors else None,
            list_mode,
            detail_mode,
            json.dumps(api_config) if api_config else None,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


@pytest.fixture(autouse=True)
def seoul(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """화면의 시간대. 마지막 확인 시각이 KST 로 나오는지 보려면 고정돼 있어야 한다."""
    monkeypatch.setenv("DISPLAY_TIMEZONE", "Asia/Seoul")
    get_settings.cache_clear()
    ui._zone.cache_clear()
    yield
    get_settings.cache_clear()
    ui._zone.cache_clear()


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


def use_stubs(discovery: Discovery | Exception) -> None:
    """등록이 부르는 둘(생성·경로 판정)을 갈아끼운다. 어느 쪽도 네트워크를 타지 않는다."""

    async def generate(list_url: str, detail_url: str, render_mode: str) -> GenerationResult:
        selectors = validate_selectors(SELECTORS)
        return GenerationResult(
            selectors=selectors,
            usage=Usage(
                provider="gemini",
                model="gemini-3.5-flash",
                input_tokens=10399,
                output_tokens=139,
                total_tokens=11229,
                latency_ms=5649,
            ),
            attempts=1,
            verification=verify_selectors(selectors, LIST_HTML, DETAIL_HTML),
        )

    async def discover(list_url: str, selectors: SelectorSet) -> Discovery:
        if isinstance(discovery, Exception):
            raise discovery
        return discovery

    app.dependency_overrides[crawlers_api.get_generator] = lambda: generate
    app.dependency_overrides[crawlers_api.get_discoverer] = lambda: discover


def test_목록과_상세를_얻는_법이_낱말로_나온다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """저장값 `api`/`static` 만으로는 어느 사이트가 어떻게 도는지 읽히지 않는다."""
    add_crawler(conn, "LG", list_mode="api", detail_mode="api", api_config=LG_CONFIG)
    add_crawler(conn, "SK", list_mode="api", detail_mode="static", api_config=SK_CONFIG)
    add_crawler(conn, "롯데", list_mode="static", detail_mode="static", selectors=SELECTORS)

    html = client.get("/ui/crawlers").text

    assert "목록 API" in html
    assert "상세 API" in html
    assert "정적 목록" in html
    assert "링크" in html
    # 항목 값으로 상세 주소를 만드는 크롤러(SK)
    assert "항목 속성" in html


def test_낱말_옆에_실제_엔드포인트가_붙는다(client: TestClient, conn: sqlite3.Connection) -> None:
    """낱말만 적으면 같은 낱말을 단 두 크롤러가 서로 다른 곳을 부르는 것이 안 보인다."""
    add_crawler(conn, "LG", list_mode="api", detail_mode="api", api_config=LG_CONFIG)

    html = client.get("/ui/crawlers").text

    assert "POST https://api.careers.lg.com/rmk/job/retrieveJobNoticesList" in html
    assert "POST https://api.careers.lg.com/rmk/job/retrieveJobNoticesDetail" in html


def test_상세_주소를_속성으로_만드는_크롤러는_그_형식을_적는다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    add_crawler(conn, "SK", list_mode="api", detail_mode="static", api_config=SK_CONFIG)

    html = client.get("/ui/crawlers").text

    assert "https://www.skcareers.com/Recruit/Detail/{id}" in html


def test_설정이_없는_api_크롤러는_그_사실을_적는다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """빈 칸은 정상으로 읽힌다. 못 가져온다는 사실을 낱말로 남긴다."""
    add_crawler(conn, "설정 없음", list_mode="api", detail_mode="api")

    html = client.get("/ui/crawlers").text

    assert "목록 API 설정이 없다" in html
    assert "상세 API 설정이 없다" in html


def test_마지막_확인_시각은_성공한_실행에서_온다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """실패로 끝난 실행은 그 경로로 실제로 가져왔다고 말해 주지 않는다."""
    crawler_id = add_crawler(conn, "LG", list_mode="api", detail_mode="api", api_config=LG_CONFIG)
    conn.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (9, ?, 'LG')", (crawler_id,))
    conn.execute(
        """
        INSERT INTO crawl_runs (workflow_id, started_at, finished_at, status)
        VALUES (9, '2026-08-25 00:10:00', '2026-08-25 00:14:00', 'success')
        """
    )
    conn.execute(
        """
        INSERT INTO crawl_runs (workflow_id, started_at, finished_at, status)
        VALUES (9, '2026-08-25 01:00:00', '2026-08-25 01:02:00', 'failed')
        """
    )
    conn.commit()

    html = client.get("/ui/crawlers").text

    # `as_time` 을 지나 KST 로 나온다. UTC 그대로면 09시가 아니라 00시로 찍힌다
    assert "2026-08-25 09:14:00 KST" in html


def test_성공한_실행이_없으면_그렇게_적는다(client: TestClient, conn: sqlite3.Connection) -> None:
    add_crawler(conn, "새 크롤러", list_mode="static", detail_mode="static", selectors=SELECTORS)

    html = client.get("/ui/crawlers").text

    assert "확인한 실행 없음" in html


def test_운영자가_목록과_상세를_따로_정한다(client: TestClient, conn: sqlite3.Connection) -> None:
    """한 값으로 함께 옮기던 토글은 `api` 크롤러를 정적으로 내려앉혔다."""
    crawler_id = add_crawler(conn, "LG", list_mode="api", detail_mode="api", api_config=LG_CONFIG)

    response = client.put(
        f"/ui/crawlers/{crawler_id}/collect-modes",
        data={"list_mode": "api", "detail_mode": "playwright"},
    )

    assert response.status_code == 200
    row = conn.execute(
        "SELECT list_mode, detail_mode FROM crawlers WHERE id = ?", (crawler_id,)
    ).fetchone()
    assert (row["list_mode"], row["detail_mode"]) == ("api", "playwright")
    assert "목록 API" in response.text


def test_모르는_경로는_거절한다(client: TestClient, conn: sqlite3.Connection) -> None:
    """저장하고 나서 실행이 실패하는 것보다 지금 거절하는 편이 낫다."""
    crawler_id = add_crawler(conn, "LG", list_mode="api", detail_mode="api", api_config=LG_CONFIG)

    response = client.put(
        f"/ui/crawlers/{crawler_id}/collect-modes",
        data={"list_mode": "selenium", "detail_mode": "api"},
    )

    assert response.status_code == 200
    assert "unknown_collect_mode" in response.text
    row = conn.execute(
        "SELECT list_mode, detail_mode FROM crawlers WHERE id = ?", (crawler_id,)
    ).fetchone()
    assert (row["list_mode"], row["detail_mode"]) == ("api", "api")


def test_저장한_경로는_등록_판정이_덮어쓰지_않는다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """다른 크롤러를 등록해 판정이 다시 돌아도 고쳐 둔 값은 그대로다."""
    crawler_id = add_crawler(conn, "LG", list_mode="api", detail_mode="api", api_config=LG_CONFIG)
    client.put(
        f"/ui/crawlers/{crawler_id}/collect-modes",
        data={"list_mode": "api", "detail_mode": "playwright"},
    )

    use_stubs(
        Discovery(
            list_mode="static",
            detail_mode="static",
            detail=document_path(DETAIL_URL, "목록 항목의 링크를 그대로 따라간다"),
            evidence="정적 목록에서 항목 12건과 상세 주소를 찾았다. 브라우저를 띄우지 않았다",
            list_count=12,
        )
    )
    client.post(
        "/ui/crawlers",
        data={"list_url": LIST_URL, "detail_url": DETAIL_URL, "default_company": "테스트"},
    )

    row = conn.execute(
        "SELECT list_mode, detail_mode FROM crawlers WHERE id = ?", (crawler_id,)
    ).fetchone()
    assert (row["list_mode"], row["detail_mode"]) == ("api", "playwright")


def test_등록_결과에_정해진_경로와_근거가_나온다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """판정만 남기면 왜 그 경로로 정해졌는지 다음 사람이 처음부터 다시 잰다."""
    use_stubs(
        Discovery(
            list_mode="static",
            detail_mode="static",
            detail=document_path(DETAIL_URL, "목록 항목의 링크를 그대로 따라간다"),
            evidence="정적 목록에서 항목 12건과 상세 주소를 찾았다. 브라우저를 띄우지 않았다",
            list_count=12,
        )
    )

    html = client.post(
        "/ui/crawlers",
        data={"list_url": LIST_URL, "detail_url": DETAIL_URL, "default_company": "테스트"},
    ).text

    assert "정해진 경로" in html
    assert "정적 목록에서 항목 12건과 상세 주소를 찾았다" in html
    assert "정적 목록" in html
    assert "링크" in html


def test_알아낸_상세_API_는_설정까지_함께_저장된다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """설정 없이 모드만 저장하면 등록만 성공하고 이후 실행이 전부 실패한다."""
    detail = DetailPath(kind="api", api=crawlers_api.parse_api_config(json.dumps(LG_CONFIG)))
    use_stubs(
        Discovery(
            list_mode="playwright",
            detail_mode="api",
            detail=detail,
            evidence="항목을 눌러 상세 API 를 알아냈고 httpx 로 다시 불러 값이 같았다",
            list_count=20,
        )
    )

    html = client.post(
        "/ui/crawlers",
        data={"list_url": LIST_URL, "detail_url": DETAIL_URL, "default_company": "테스트"},
    ).text

    row = conn.execute("SELECT * FROM crawlers ORDER BY id DESC LIMIT 1").fetchone()
    assert (row["list_mode"], row["detail_mode"]) == ("playwright", "api")
    assert row["api_config_json"]
    assert "상세 API" in html
    assert "retrieveJobNoticesDetail" in html


def test_경로를_못_정해도_등록은_남고_사유가_보인다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """여기서 예외를 내면 방금 만든 셀렉터까지 같이 사라진다. 고칠 대상마저 없어진다."""
    use_stubs(
        Discovery(
            list_mode="playwright",
            evidence="정적 목록에 항목 0건, 렌더 후 16건, 항목에 상세 주소가 없어 클릭했다",
            failure="detail_unreachable",
            reason="클릭 뒤 나간 요청 중 이 공고를 지목한 것이 없다. 상세 경로를 손으로 적는다",
            list_count=16,
        )
    )

    html = client.post(
        "/ui/crawlers",
        data={"list_url": LIST_URL, "detail_url": DETAIL_URL, "default_company": "테스트"},
    ).text

    assert conn.execute("SELECT COUNT(*) FROM crawlers").fetchone()[0] == 1
    assert "detail_unreachable" in html
    # 사유 이름만으로는 무엇을 할지 모른다. 다음 행동이 함께 붙는다 (`app/api/ui.py`)
    assert ui.NEXT_STEPS["detail_unreachable"] in html


def test_판정_중_예외가_나도_등록은_끝난다(client: TestClient, conn: sqlite3.Connection) -> None:
    """브라우저가 없는 배포에서도 등록은 되고, 못 알아냈다는 사실만 화면에 남는다."""
    use_stubs(RuntimeError("Chromium 을 띄우지 못했다"))

    response = client.post(
        "/ui/crawlers",
        data={"list_url": LIST_URL, "detail_url": DETAIL_URL, "default_company": "테스트"},
    )

    assert response.status_code == 200
    assert conn.execute("SELECT COUNT(*) FROM crawlers").fetchone()[0] == 1
    assert "경로를 판정하는 중에 실패했다" in response.text
