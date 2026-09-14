"""AI 수정 API (17.2).

확인하는 것은 둘이다.

- 고치기 결과가 **전과 후의 매칭 개수를 함께** 돌려주는가. 하나만 주면 운영자는 무엇이
  나아졌는지 알 수 없다
- 호출만으로 `crawlers.selectors_json` 이 바뀌지 않는가. 저장은 운영자가 "셀렉터 저장" 을
  누를 때만이다 (`.claude/rules/llm.md`)

Gemini 도 실사이트도 부르지 않는다. 고치기 의존성을 갈아끼우고 저장된 픽스처로 판정한다.
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
from app.crawler.fetcher import TransportError
from app.main import app
from app.selector.generator import SelectorGenerationError, Usage
from app.selector.repair import RepairOutcome, SelectorRepairError, repair_targets
from app.selector.schema import SelectorSet, validate_selectors
from app.selector.verify import verify_selectors
from tests.test_selector_repair import BROKEN, DETAIL_HTML, LIST_HTML

LIST_URL = "https://group.example.test/recruit"
DETAIL_URL = "https://group.example.test/recruit/view/2001"

FIXED: dict[str, Any] = json.loads(json.dumps(BROKEN))
FIXED["list"]["item"] = "ul.job-card-list > li.job-card"

USAGE = Usage(
    provider="gemini",
    model="gemini-3.5-flash",
    input_tokens=8123,
    output_tokens=142,
    total_tokens=8265,
    latency_ms=4210,
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


def insert_crawler(
    conn: sqlite3.Connection, selectors: dict[str, Any] = BROKEN, detail_url: str = DETAIL_URL
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO crawlers
               (name, list_url, detail_url, selectors_json, status, list_mode)
        VALUES ('그룹 채용', ?, ?, ?, 'draft', 'static')
        """,
        (LIST_URL, detail_url or None, json.dumps(selectors, ensure_ascii=False)),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def outcome_for(
    selectors: SelectorSet,
    proposal: dict[str, Any] = FIXED,
    detail_html: str = DETAIL_HTML,
) -> RepairOutcome:
    """실제 고치기와 같은 계산을 픽스처로 만든다. 모델 응답만 정해 준다."""
    before = verify_selectors(selectors, LIST_HTML, detail_html)
    targets = repair_targets(before, has_detail_html=bool(detail_html))
    repaired = validate_selectors(proposal)
    after = verify_selectors(repaired, LIST_HTML, detail_html)
    remaining = set(repair_targets(after, has_detail_html=bool(detail_html)))
    return RepairOutcome(
        selectors=repaired,
        before=before,
        after=after,
        usage=USAGE,
        attempts=1,
        targets=targets,
        # 힌트 없이 부른 것과 같다. 대상이 곧 실패였던 필드다
        failed_targets=targets,
        changes=[],
        unresolved=[name for name in targets if name in remaining],
    )


def use_repairer(result: Any) -> list[tuple[str, str, str, str]]:
    """고치기 의존성을 갈아끼운다. `result` 가 예외면 그것을 던진다.

    받은 힌트도 함께 기록한다. 화면이 넣은 값이 여기까지 오는지는 그것으로만 알 수 있다.
    """
    called: list[tuple[str, str, str, str]] = []

    async def repair(
        list_url: str,
        detail_url: str,
        render_mode: str,
        selectors: SelectorSet,
        *,
        hint: str = "",
    ) -> RepairOutcome:
        called.append((list_url, detail_url, render_mode, hint))
        if isinstance(result, Exception):
            raise result
        if callable(result):
            return result(selectors)
        return result

    app.dependency_overrides[crawlers_api.get_repairer] = lambda: repair
    return called


def stored_selectors(conn: sqlite3.Connection, crawler_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT selectors_json FROM crawlers WHERE id = ?", (crawler_id,)).fetchone()
    return dict(json.loads(row["selectors_json"]))


# 전/후 --------------------------------------------------------------------


def test_repair_returns_before_and_after_matches(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    crawler_id = insert_crawler(conn)
    use_repairer(outcome_for)

    response = client.post(f"/api/crawlers/{crawler_id}/repair")

    assert response.status_code == 200
    body = response.json()
    # 항목이 계열사 목록을 잡고 있어 항목 안에서 아무것도 안 나왔다
    assert body["before_matches"]["list.item"] == 4
    assert body["before_matches"]["list.title"] == 0
    # 고친 뒤에는 공고 카드 3건과 그 안의 필드가 나온다
    assert body["after_matches"]["list.item"] == 3
    assert body["after_matches"]["list.title"] == 3
    assert body["after_matches"]["list.link"] == 3
    assert body["after_matches"]["list.date"] == 3
    # 전과 후가 같은 필드 목록이어야 나란히 읽힌다
    assert body["before_matches"].keys() == body["after_matches"].keys()


def test_repair_reports_what_it_fixed_and_what_it_did_not(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    crawler_id = insert_crawler(conn)
    use_repairer(outcome_for)

    body = client.post(f"/api/crawlers/{crawler_id}/repair").json()

    assert body["targets"] == [
        "list.item",
        "list.title",
        "list.link",
        "list.date",
        "list.company_name",
    ]
    assert body["repaired"] == body["targets"]
    assert body["unresolved"] == []


def test_a_repair_that_did_not_work_says_so(client: TestClient, conn: sqlite3.Connection) -> None:
    """고친 뒤에도 실패가 남으면 그대로 말한다. 억지로 성공으로 만들지 않는다."""
    crawler_id = insert_crawler(conn)
    still_wrong = json.loads(json.dumps(BROKEN))
    still_wrong["list"]["item"] = "nav.family ul li"
    use_repairer(lambda selectors: outcome_for(selectors, still_wrong))

    body = client.post(f"/api/crawlers/{crawler_id}/repair").json()

    assert body["unresolved"]
    assert body["repaired"] == []
    assert body["after_matches"]["list.title"] == 0


def test_skipped_fields_are_not_reported_as_failures(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    crawler_id = insert_crawler(conn)
    use_repairer(outcome_for)

    body = client.post(f"/api/crawlers/{crawler_id}/repair").json()

    assert "detail.qualifications" in body["skipped_fields"]
    assert "detail.department" in body["skipped_fields"]
    assert not set(body["targets"]) & set(body["skipped_fields"])


def test_usage_is_reported(client: TestClient, conn: sqlite3.Connection) -> None:
    crawler_id = insert_crawler(conn)
    use_repairer(outcome_for)

    body = client.post(f"/api/crawlers/{crawler_id}/repair").json()

    assert body["usage"]["model"] == "gemini-3.5-flash"
    assert body["usage"]["input_tokens"] == 8123
    assert body["usage"]["latency_ms"] == 4210


# 저장하지 않는다 ------------------------------------------------------------


def test_repair_alone_does_not_change_stored_selectors(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    crawler_id = insert_crawler(conn)
    before = stored_selectors(conn, crawler_id)
    use_repairer(outcome_for)

    body = client.post(f"/api/crawlers/{crawler_id}/repair").json()

    assert body["saved"] is False
    # 응답의 셀렉터는 고쳐졌는데 DB 는 그대로다
    assert body["selectors"]["list"]["item"] == "ul.job-card-list > li.job-card"
    assert stored_selectors(conn, crawler_id) == before
    assert stored_selectors(conn, crawler_id)["list"]["item"] == "ul.family-group li"


def test_saving_afterwards_is_what_changes_the_database(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """저장은 기존 경로 하나뿐이다. 고치기가 두 번째 저장 경로가 되지 않는다."""
    crawler_id = insert_crawler(conn)
    use_repairer(outcome_for)

    repaired = client.post(f"/api/crawlers/{crawler_id}/repair").json()["selectors"]
    assert stored_selectors(conn, crawler_id)["list"]["item"] == "ul.family-group li"

    saved = client.put(f"/api/crawlers/{crawler_id}/selectors", json=repaired)

    assert saved.status_code == 200
    assert stored_selectors(conn, crawler_id)["list"]["item"] == "ul.job-card-list > li.job-card"


def test_repair_does_not_change_the_crawler_status(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    crawler_id = insert_crawler(conn)
    use_repairer(outcome_for)

    body = client.post(f"/api/crawlers/{crawler_id}/repair").json()

    assert body["status"] == "draft"
    row = conn.execute("SELECT status FROM crawlers WHERE id = ?", (crawler_id,)).fetchone()
    assert row["status"] == "draft"


def test_repair_uses_the_saved_render_mode(client: TestClient, conn: sqlite3.Connection) -> None:
    crawler_id = insert_crawler(conn)
    conn.execute("UPDATE crawlers SET list_mode = 'playwright' WHERE id = ?", (crawler_id,))
    conn.commit()
    called = use_repairer(outcome_for)

    client.post(f"/api/crawlers/{crawler_id}/repair")

    assert called == [(LIST_URL, DETAIL_URL, "playwright", "")]


# 운영자 힌트 (20.1) ---------------------------------------------------------


def test_a_hint_in_the_body_reaches_the_repairer(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """운영자가 F12 로 딴 경로. 그대로 저장되는 것이 아니라 위치 단서로 실려 나간다."""
    crawler_id = insert_crawler(conn)
    called = use_repairer(outcome_for)
    hint = "#root > div > main > div.MuiBox-root.css-1jelp97 > div:nth-child(2) > div"

    client.post(f"/api/crawlers/{crawler_id}/repair", json={"hint": hint})

    assert [row[3] for row in called] == [hint]


def test_a_free_text_hint_is_carried_as_is(client: TestClient, conn: sqlite3.Connection) -> None:
    """셀렉터가 아닐 수도 있다. 판정하지 않고 그대로 넘긴다."""
    crawler_id = insert_crawler(conn)
    called = use_repairer(outcome_for)

    client.post(
        f"/api/crawlers/{crawler_id}/repair",
        json={"hint": "마감일은 목록 두 번째 줄에 있다"},
    )

    assert called[0][3] == "마감일은 목록 두 번째 줄에 있다"


def test_the_body_is_optional(client: TestClient, conn: sqlite3.Connection) -> None:
    """힌트가 생기기 전과 같이 본문 없이 불러도 된다."""
    crawler_id = insert_crawler(conn)
    called = use_repairer(outcome_for)

    response = client.post(f"/api/crawlers/{crawler_id}/repair")

    assert response.status_code == 200
    assert called[0][3] == ""


# 실패 ----------------------------------------------------------------------


def test_unknown_crawler_is_404(client: TestClient) -> None:
    use_repairer(outcome_for)

    assert client.post("/api/crawlers/9999/repair").status_code == 404


def test_nothing_to_repair_is_409(client: TestClient, conn: sqlite3.Connection) -> None:
    crawler_id = insert_crawler(conn, FIXED)
    use_repairer(SelectorRepairError("nothing_to_repair", "실패한 필드가 없다"))

    response = client.post(f"/api/crawlers/{crawler_id}/repair")

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "nothing_to_repair"


def test_a_rate_limited_model_call_surfaces_the_code(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """Gemini 무료 한도는 분당 20회다. 429 를 삼키지 않고 그대로 올린다."""
    crawler_id = insert_crawler(conn)
    use_repairer(SelectorGenerationError("api_error", "Gemini 호출 실패(429): RESOURCE_EXHAUSTED"))

    response = client.post(f"/api/crawlers/{crawler_id}/repair")

    assert response.status_code == 502
    assert response.json()["detail"]["reason"] == "api_error"
    assert "429" in response.json()["detail"]["message"]


def test_a_fetch_failure_is_reported_as_transport(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    crawler_id = insert_crawler(conn)
    use_repairer(TransportError("연결이 끊겼다"))

    response = client.post(f"/api/crawlers/{crawler_id}/repair")

    assert response.status_code == 502
    assert response.json()["detail"]["reason"] == "transport"


def test_a_failed_repair_leaves_the_database_alone(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    crawler_id = insert_crawler(conn)
    before = stored_selectors(conn, crawler_id)
    use_repairer(SelectorGenerationError("api_error", "Gemini 호출 실패(429): 한도 초과"))

    client.post(f"/api/crawlers/{crawler_id}/repair")

    assert stored_selectors(conn, crawler_id) == before


def test_the_failed_count_does_not_grow_with_the_hint(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """힌트가 들어오면 대상이 넓어진다. 화면이 "실패한 필드 N개" 로 셀 것은 실패였던 쪽이다."""
    crawler_id = insert_crawler(conn)

    def widened(selectors: SelectorSet) -> RepairOutcome:
        result = outcome_for(selectors)
        return RepairOutcome(
            selectors=result.selectors,
            before=result.before,
            after=result.after,
            usage=result.usage,
            attempts=result.attempts,
            targets=[*result.targets, "detail.title", "detail.body"],
            failed_targets=result.targets,
            changes=result.changes,
            unresolved=result.unresolved,
        )

    use_repairer(widened)

    body = client.post(f"/api/crawlers/{crawler_id}/repair", json={"hint": "제목이 틀렸다"}).json()

    assert len(body["targets"]) > len(body["failed_targets"])
    assert "detail.title" not in body["failed_targets"]
