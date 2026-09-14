"""크롤러 등록·수동 보정 API 테스트.

Gemini 도 실사이트도 부르지 않는다. 생성 의존성을 갈아끼우고, 확인하는 것은 `crawlers` 행이
어떤 상태로 남는가다. 셀렉터 자체는 2.3.V 에서 실제 생성 호출로 얻은 것을 쓴다.
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
from app.crawler.fetcher import RobotsDisallowedError, TransportError
from app.main import app
from app.selector.detail_path import document_path
from app.selector.discovery import Discovery
from app.selector.generator import GenerationResult, SelectorGenerationError, Usage
from app.selector.schema import SPLIT_DETAIL_FIELDS, SelectorSet, validate_selectors
from app.selector.verify import verify_selectors

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
LIST_HTML = (FIXTURES / "pythonorg-jobs-list-20260821.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "pythonorg-job-detail-20260821.html").read_text(encoding="utf-8")
# 목록 컨테이너만 있고 항목은 스크립트가 채우는 사이트. 정적 HTML 에는 공고가 없다
SHELL_HTML = (FIXTURES / "js-rendered-list-shell-20260822.html").read_text(encoding="utf-8")

LIST_URL = "https://www.python.org/jobs/"
DETAIL_URL = "https://www.python.org/jobs/8126/"

GENERATED: dict[str, Any] = {
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

USAGE = Usage(
    provider="gemini",
    model="gemini-3.5-flash",
    input_tokens=10399,
    output_tokens=139,
    total_tokens=11229,
    latency_ms=5649,
)


def stored(payload: dict[str, Any]) -> dict[str, Any]:
    """저장되는 모양. 선택 필드는 안 적어도 빈 문자열로 채워져 저장된다."""
    filled = json.loads(json.dumps(payload))
    filled["list"].setdefault("company_name", "")
    filled["list"].setdefault("link_template", "")
    filled["detail"].setdefault("company_name", "")
    for name in SPLIT_DETAIL_FIELDS:
        filled["detail"].setdefault(name, "")
    return filled


def result_for(
    payload: dict[str, Any],
    list_html: str = LIST_HTML,
    detail_html: str = DETAIL_HTML,
) -> GenerationResult:
    selectors = validate_selectors(payload)
    return GenerationResult(
        selectors=selectors,
        usage=USAGE,
        attempts=1,
        verification=verify_selectors(selectors, list_html, detail_html),
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
        """요청마다 같은 파일에 새 연결을 연다. 운영 경로와 같은 모양이다."""
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


def use_generator(result: Any) -> list[str]:
    """생성 의존성을 갈아끼운다. `result` 가 예외면 그것을 던진다.

    돌려주는 목록에는 생성이 어떤 `render_mode` 로 불렸는지가 쌓인다.
    """
    called_with: list[str] = []

    async def generate(list_url: str, detail_url: str, render_mode: str) -> GenerationResult:
        called_with.append(render_mode)
        if isinstance(result, Exception):
            raise result
        return result

    app.dependency_overrides[crawlers_api.get_generator] = lambda: generate
    stub_discoverer()
    return called_with


def rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM crawlers").fetchall()


def test_registration_stores_a_draft_row(client: TestClient, conn: sqlite3.Connection) -> None:
    use_generator(result_for(GENERATED))

    response = client.post(
        "/api/crawlers",
        json={"list_url": LIST_URL, "detail_url": DETAIL_URL, "name": "python.org 채용"},
    )

    assert response.status_code == 201
    saved = rows(conn)
    assert len(saved) == 1
    assert saved[0]["status"] == "draft"
    assert saved[0]["name"] == "python.org 채용"
    assert saved[0]["list_url"] == LIST_URL
    assert json.loads(saved[0]["selectors_json"]) == stored(GENERATED)
    assert response.json()["id"] == saved[0]["id"]


def test_registration_reports_matches_and_no_failed_field(client: TestClient) -> None:
    use_generator(result_for(GENERATED))

    body = client.post("/api/crawlers", json={"list_url": LIST_URL, "detail_url": DETAIL_URL})

    payload = body.json()
    assert payload["failed_fields"] == []
    assert payload["matches"]["list.item"] > 1
    assert payload["usage"]["model"] == "gemini-3.5-flash"
    assert payload["usage"]["input_tokens"] == 10399
    assert payload["name"] == "www.python.org"


def test_failed_field_is_surfaced_but_the_draft_is_kept(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """실패한 필드가 있어도 행은 남는다. 그 필드만 손으로 고치는 것이 첫 수단이다."""
    broken = json.loads(json.dumps(GENERATED))
    broken["list"]["date"] = "span.published-on"
    use_generator(result_for(broken))

    response = client.post("/api/crawlers", json={"list_url": LIST_URL, "detail_url": DETAIL_URL})

    assert response.json()["failed_fields"] == ["list.date"]
    assert rows(conn)[0]["status"] == "draft"


def test_registration_stores_the_operator_company(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """운영자가 적은 회사명은 `crawlers` 에만 저장된다. 추출 결과가 아니라 raw 로 가지 않는다."""
    use_generator(result_for(GENERATED))

    response = client.post(
        "/api/crawlers",
        json={
            "list_url": LIST_URL,
            "detail_url": DETAIL_URL,
            "default_company": "  삼성전기  ",
        },
    )

    assert response.status_code == 201
    assert response.json()["default_company"] == "삼성전기"
    assert rows(conn)[0]["default_company"] == "삼성전기"


def test_registration_without_a_company_leaves_null(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """안 적으면 NULL 이다. 빈 문자열이면 "회사명이 있다" 와 구분되지 않는다."""
    use_generator(result_for(GENERATED))

    response = client.post("/api/crawlers", json={"list_url": LIST_URL, "detail_url": DETAIL_URL})

    assert response.json()["default_company"] is None
    assert rows(conn)[0]["default_company"] is None


def test_company_can_be_corrected_after_registration(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    use_generator(result_for(GENERATED))
    created = client.post(
        "/api/crawlers",
        json={"list_url": LIST_URL, "detail_url": DETAIL_URL, "default_company": "삼성전자"},
    ).json()

    response = client.put(
        f"/api/crawlers/{created['id']}/company", json={"default_company": "삼성전기"}
    )

    assert response.status_code == 200
    assert response.json()["default_company"] == "삼성전기"
    assert rows(conn)[0]["default_company"] == "삼성전기"


def test_clearing_the_company_stores_null(client: TestClient, conn: sqlite3.Connection) -> None:
    use_generator(result_for(GENERATED))
    created = client.post(
        "/api/crawlers",
        json={"list_url": LIST_URL, "detail_url": DETAIL_URL, "default_company": "삼성전자"},
    ).json()

    client.put(f"/api/crawlers/{created['id']}/company", json={"default_company": "   "})

    assert rows(conn)[0]["default_company"] is None


def test_company_update_on_a_missing_crawler_is_404(client: TestClient) -> None:
    response = client.put("/api/crawlers/999/company", json={"default_company": "삼성전기"})

    assert response.status_code == 404


def test_robots_disallow_refuses_registration(client: TestClient, conn: sqlite3.Connection) -> None:
    use_generator(RobotsDisallowedError("robots.txt 가 막은 경로다"))

    response = client.post("/api/crawlers", json={"list_url": LIST_URL, "detail_url": DETAIL_URL})

    assert response.status_code == 400
    assert response.json()["detail"]["reason"] == "robots"
    assert rows(conn) == []


def test_transport_failure_leaves_no_row(client: TestClient, conn: sqlite3.Connection) -> None:
    use_generator(TransportError("전송 실패"))

    response = client.post("/api/crawlers", json={"list_url": LIST_URL, "detail_url": DETAIL_URL})

    assert response.status_code == 502
    assert response.json()["detail"]["reason"] == "transport"
    assert rows(conn) == []


def test_missing_api_key_is_a_server_side_reason(client: TestClient) -> None:
    use_generator(SelectorGenerationError("no_api_key", "GEMINI_API_KEY 가 비어 있다"))

    response = client.post("/api/crawlers", json={"list_url": LIST_URL, "detail_url": DETAIL_URL})

    assert response.status_code == 500
    assert response.json()["detail"]["reason"] == "no_api_key"


def test_manual_edit_changes_selectors_and_keeps_status(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    use_generator(result_for(GENERATED))
    created = client.post(
        "/api/crawlers", json={"list_url": LIST_URL, "detail_url": DETAIL_URL}
    ).json()
    conn.execute("UPDATE crawlers SET status = 'tested' WHERE id = ?", (created["id"],))

    edited = json.loads(json.dumps(GENERATED))
    edited["list"]["date"] = "time[datetime]"
    response = client.put(f"/api/crawlers/{created['id']}/selectors", json=edited)

    assert response.status_code == 200
    saved = rows(conn)[0]
    assert json.loads(saved["selectors_json"])["list"]["date"] == "time[datetime]"
    assert saved["status"] == "tested"
    assert response.json()["status"] == "tested"


def test_manual_edit_does_not_regenerate(client: TestClient, conn: sqlite3.Connection) -> None:
    """편집된 셀렉터를 요청 없이 다시 생성하지 않는다 (rules/llm.md)."""
    use_generator(result_for(GENERATED))
    created = client.post(
        "/api/crawlers", json={"list_url": LIST_URL, "detail_url": DETAIL_URL}
    ).json()
    use_generator(AssertionError("수동 보정은 생성을 부르지 않는다"))

    response = client.put(f"/api/crawlers/{created['id']}/selectors", json=GENERATED)

    assert response.status_code == 200


def test_manual_edit_rejects_a_field_the_schema_does_not_have(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    use_generator(result_for(GENERATED))
    created = client.post(
        "/api/crawlers", json={"list_url": LIST_URL, "detail_url": DETAIL_URL}
    ).json()

    broken = json.loads(json.dumps(GENERATED))
    broken["list"]["links"] = "a"
    response = client.put(f"/api/crawlers/{created['id']}/selectors", json=broken)

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "unknown_field"
    assert json.loads(rows(conn)[0]["selectors_json"]) == stored(GENERATED)


def test_manual_edit_on_a_missing_crawler_is_404(client: TestClient) -> None:
    response = client.put("/api/crawlers/999/selectors", json=GENERATED)

    assert response.status_code == 404


def make_test_run(conn: sqlite3.Connection, crawler_id: int) -> int:
    """승격 전 테스트 실행 기록 하나. `workflow_id` 없이 크롤러만 가리킨다."""
    cursor = conn.execute(
        "INSERT INTO crawl_runs (crawler_id, status, success_count) VALUES (?, 'success', 1)",
        (crawler_id,),
    )
    return int(cursor.lastrowid or 0)


def make_workflow(conn: sqlite3.Connection, crawler_id: int) -> int:
    cursor = conn.execute(
        "INSERT INTO workflows (crawler_id, name) VALUES (?, '테스트 워크플로우')",
        (crawler_id,),
    )
    conn.execute("UPDATE crawlers SET status = 'promoted' WHERE id = ?", (crawler_id,))
    return int(cursor.lastrowid or 0)


def make_collected_rows(conn: sqlite3.Connection, workflow_id: int) -> None:
    """수집된 공고 한 건. 크롤러가 아니라 워크플로우에 매달려 있다."""
    cursor = conn.execute(
        """
        INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash)
        VALUES (?, 'https://example.com/1', '{}', 'hash-1')
        """,
        (workflow_id,),
    )
    conn.execute(
        """
        INSERT INTO normalized_jobs (raw_job_id, title, source_url)
        VALUES (?, '공고', 'https://example.com/1')
        """,
        (int(cursor.lastrowid or 0),),
    )


def counts(conn: sqlite3.Connection) -> tuple[int, int]:
    raw = conn.execute("SELECT count(*) AS n FROM raw_jobs").fetchone()["n"]
    normalized = conn.execute("SELECT count(*) AS n FROM normalized_jobs").fetchone()["n"]
    return int(raw), int(normalized)


def register(client: TestClient) -> int:
    use_generator(result_for(GENERATED))
    return int(
        client.post("/api/crawlers", json={"list_url": LIST_URL, "detail_url": DETAIL_URL}).json()[
            "id"
        ]
    )


def test_a_draft_crawler_is_deleted(client: TestClient, conn: sqlite3.Connection) -> None:
    crawler_id = register(client)

    response = client.delete(f"/api/crawlers/{crawler_id}")

    assert response.status_code == 200
    assert response.json()["id"] == crawler_id
    assert rows(conn) == []


def test_deleting_a_crawler_drops_its_test_runs(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """테스트 실행 기록은 이 크롤러만 가리킨다. 정의가 없으면 읽을 수 없어 함께 지운다."""
    crawler_id = register(client)
    make_test_run(conn, crawler_id)
    make_test_run(conn, crawler_id)

    response = client.delete(f"/api/crawlers/{crawler_id}")

    assert response.json()["deleted_test_runs"] == 2
    assert conn.execute("SELECT count(*) AS n FROM crawl_runs").fetchone()["n"] == 0


def test_a_promoted_crawler_is_refused(client: TestClient, conn: sqlite3.Connection) -> None:
    crawler_id = register(client)
    make_workflow(conn, crawler_id)

    response = client.delete(f"/api/crawlers/{crawler_id}")

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "has_workflow"
    assert "워크플로우를 먼저" in response.json()["detail"]["message"]
    assert len(rows(conn)) == 1


def test_a_promoted_crawler_without_a_workflow_is_still_refused(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """워크플로우 행이 없어도 상태가 promoted 면 지우지 않는다. 상태부터 설명돼야 한다."""
    crawler_id = register(client)
    conn.execute("UPDATE crawlers SET status = 'promoted' WHERE id = ?", (crawler_id,))

    response = client.delete(f"/api/crawlers/{crawler_id}")

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "promoted"
    assert len(rows(conn)) == 1


def test_refused_delete_keeps_the_collected_rows(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """거절된 삭제는 수집 데이터를 건드리지 않는다 (rules/data-safety.md)."""
    crawler_id = register(client)
    workflow_id = make_workflow(conn, crawler_id)
    make_collected_rows(conn, workflow_id)
    before = counts(conn)

    client.delete(f"/api/crawlers/{crawler_id}")

    assert counts(conn) == before == (1, 1)


def test_delete_leaves_the_collected_rows_of_other_crawlers(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """지워지는 크롤러와 무관하게 raw_jobs·normalized_jobs 행 수는 그대로다."""
    kept = register(client)
    workflow_id = make_workflow(conn, kept)
    make_collected_rows(conn, workflow_id)
    doomed = register(client)
    before = counts(conn)

    response = client.delete(f"/api/crawlers/{doomed}")

    assert response.status_code == 200
    assert counts(conn) == before == (1, 1)
    assert [row["id"] for row in rows(conn)] == [kept]


def test_deleting_a_missing_crawler_is_404(client: TestClient) -> None:
    response = client.delete("/api/crawlers/999")

    assert response.status_code == 404


# 껍데기 페이지를 보고 생성된 셀렉터. 그럴듯하지만 어느 것도 노드를 잡지 못한다.
SHELL_GENERATED: dict[str, Any] = {
    "list": {
        "item": "#applyList li",
        "title": "#applyList li .tit_job",
        "link": "#applyList li a",
        "date": "#applyList li .date",
    },
    "detail": {
        "title": "h1.tit",
        "body": "#container",
        "qualifications": "",
        "recruitment_end_at": "",
        "department": "",
    },
}


def test_a_whole_list_miss_fails_and_stores_nothing(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """목록 4개 필드가 전부 0개 매칭이면 201 이 아니라 실패다. 행도 남지 않는다."""
    use_generator(result_for(SHELL_GENERATED, list_html=SHELL_HTML, detail_html=SHELL_HTML))

    response = client.post("/api/crawlers", json={"list_url": LIST_URL, "detail_url": DETAIL_URL})

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "list_not_found"
    assert rows(conn) == []


def test_the_whole_list_miss_names_the_failed_fields_and_a_next_step(
    client: TestClient,
) -> None:
    """다음에 무엇을 할 수 있는지가 사유에 있어야 한다."""
    use_generator(result_for(SHELL_GENERATED, list_html=SHELL_HTML, detail_html=SHELL_HTML))

    response = client.post("/api/crawlers", json={"list_url": LIST_URL, "detail_url": DETAIL_URL})

    detail = response.json()["detail"]
    assert "목록을 찾지 못했다" in detail["message"]
    for field in ("list.item", "list.title", "list.link", "list.date"):
        assert field in detail["message"]
        assert detail["matches"][field] == 0


def test_matching_detail_fields_do_not_rescue_a_missing_list(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """상세가 다 잡혀도 목록이 없으면 못 쓰는 크롤러다. 판정은 목록만 본다."""
    result = result_for(SHELL_GENERATED, list_html=SHELL_HTML, detail_html=SHELL_HTML)
    assert "detail.title" not in result.verification.failed
    use_generator(result)

    response = client.post("/api/crawlers", json={"list_url": LIST_URL, "detail_url": DETAIL_URL})

    assert response.status_code == 422
    assert rows(conn) == []


def test_a_partial_list_miss_is_still_stored(client: TestClient, conn: sqlite3.Connection) -> None:
    """일부 목록 필드만 실패한 것은 사람이 손으로 고칠 수 있다. 저장하고 실패 필드를 알린다."""
    partial = json.loads(json.dumps(GENERATED))
    partial["list"]["date"] = "span.published-on"
    partial["list"]["title"] = "span.no-such-name"
    use_generator(result_for(partial))

    response = client.post("/api/crawlers", json={"list_url": LIST_URL, "detail_url": DETAIL_URL})

    assert response.status_code == 201
    assert response.json()["failed_fields"] == ["list.title", "list.date"]
    assert response.json()["matches"]["list.item"] > 0
    assert len(rows(conn)) == 1


def test_only_the_item_selector_matching_is_refused(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """item 만 잡히고 그 안이 전부 0이면 저장하지 않는다 (12.5).

    12.2 까지는 저장했다. 항목이 잡혔으니 목록은 있다고 본 것인데, 제목도 링크도 날짜도
    없는 공고만 나오는 크롤러라 실행해도 쓸 값이 하나도 안 나온다. 자세한 것은
    `tests/test_empty_list_items.py` 가 본다.
    """
    partial = json.loads(json.dumps(GENERATED))
    partial["list"]["title"] = "span.no-such-name"
    partial["list"]["link"] = "a.no-such-link"
    partial["list"]["date"] = "span.published-on"
    use_generator(result_for(partial))

    response = client.post("/api/crawlers", json={"list_url": LIST_URL, "detail_url": DETAIL_URL})

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "list_fields_not_found"
    assert rows(conn) == []


def test_registration_leaves_the_mode_to_the_judgement(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """아무것도 고르지 않은 등록은 생성 경로를 판정에 맡긴다.

    빈 값이 그대로 생성에 넘어가야 등록이 정적으로 먼저 해 보고 안 되면 렌더로 올릴 수 있다.
    모드별로 나눠 보는 것은 `tests/test_render_default.py` 다.
    """
    called_with = use_generator(result_for(GENERATED))

    response = client.post("/api/crawlers", json={"list_url": LIST_URL, "detail_url": DETAIL_URL})

    assert response.status_code == 201
    assert called_with == [""]
    assert (rows(conn)[0]["list_mode"], rows(conn)[0]["detail_mode"]) == ("static", "static")
    assert response.json()["render_mode"] == "static"


def test_registration_can_start_in_render_mode(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """렌더로 등록하면 셀렉터 생성도 렌더된 HTML 을 본다.

    저장되는 목록 모드는 운영자가 고른 그대로다. 상세 모드는 경로 판정이 정한다 — 목록이
    렌더여야 하는 사이트도 상세 문서는 정적으로 열리는 것이 보통이고, 둘을 한 값으로 묶으면
    실행마다 필요 없는 브라우저가 하나 뜬다 (`app/selector/discovery.py`).
    """
    called_with = use_generator(result_for(GENERATED))

    response = client.post(
        "/api/crawlers",
        json={"list_url": LIST_URL, "detail_url": DETAIL_URL, "render_mode": "playwright"},
    )

    assert response.status_code == 201
    assert called_with == ["playwright"]
    assert (rows(conn)[0]["list_mode"], rows(conn)[0]["detail_mode"]) == (
        "playwright",
        "static",
    )


def test_registration_refuses_an_unknown_render_mode(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """모르는 값을 조용히 static 으로 바꾸지 않는다. 올린 줄 알고 기다리게 된다."""
    use_generator(result_for(GENERATED))

    response = client.post(
        "/api/crawlers",
        json={"list_url": LIST_URL, "detail_url": DETAIL_URL, "render_mode": "selenium"},
    )

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "unknown_render_mode"
    assert rows(conn) == []


def test_render_mode_can_be_switched(client: TestClient, conn: sqlite3.Connection) -> None:
    use_generator(result_for(GENERATED))
    crawler_id = client.post(
        "/api/crawlers", json={"list_url": LIST_URL, "detail_url": DETAIL_URL}
    ).json()["id"]

    response = client.put(
        f"/api/crawlers/{crawler_id}/render-mode", json={"render_mode": "playwright"}
    )

    assert response.status_code == 200
    assert response.json() == {"id": crawler_id, "render_mode": "playwright"}
    assert (rows(conn)[0]["list_mode"], rows(conn)[0]["detail_mode"]) == (
        "playwright",
        "playwright",
    )


def test_switching_render_mode_keeps_the_selectors(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """모드를 바꾼다고 셀렉터를 다시 만들지 않는다. 손으로 고친 값이 날아가면 안 된다."""
    use_generator(result_for(GENERATED))
    crawler_id = client.post(
        "/api/crawlers", json={"list_url": LIST_URL, "detail_url": DETAIL_URL}
    ).json()["id"]

    client.put(f"/api/crawlers/{crawler_id}/render-mode", json={"render_mode": "playwright"})

    assert json.loads(rows(conn)[0]["selectors_json"]) == stored(GENERATED)


def test_switching_render_mode_on_a_missing_crawler_is_404(client: TestClient) -> None:
    response = client.put("/api/crawlers/999/render-mode", json={"render_mode": "playwright"})

    assert response.status_code == 404


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
