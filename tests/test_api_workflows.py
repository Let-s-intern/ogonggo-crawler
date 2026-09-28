"""워크플로우 승격·목록·주기 변경·중지·재개 API 테스트.

실사이트에 나가지 않는다. 이 라우터는 DB 상태와 스케줄러 잡만 만지므로 fetch 클라이언트가
필요 없다.

스케줄러는 시작하지 않은 진짜 `WorkflowScheduler` 를 쓴다. 잡은 pending 으로 쌓이고 조회·갱신·
제거가 그대로 동작해서, 확인하려는 것("테이블을 바꾼 요청이 잡까지 갔는가")을 가짜로 바꾸지
않고 볼 수 있다.
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi.testclient import TestClient

from app import db
from app.api import workflows as workflows_api
from app.main import app
from app.scheduler import WorkflowScheduler
from tests.schedule_helpers import in_round

LIST_URL = "https://www.python.org/jobs/"


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
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
    app.dependency_overrides[workflows_api.get_workflow_scheduler] = lambda: scheduler
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def add_crawler(conn: sqlite3.Connection, status: str) -> int:
    cursor = conn.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES (?, ?, ?)",
        ("python.org", LIST_URL, status),
    )
    return int(cursor.lastrowid or 0)


def crawler_status(conn: sqlite3.Connection, crawler_id: int) -> str:
    row = conn.execute("SELECT status FROM crawlers WHERE id = ?", (crawler_id,)).fetchone()
    return str(row["status"])


def workflow(conn: sqlite3.Connection, workflow_id: int) -> sqlite3.Row:
    return conn.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()


def test_tested_크롤러는_승격되고_promoted_가_된다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    crawler_id = add_crawler(conn, "tested")

    response = client.post(
        "/api/workflows",
        json={
            "crawler_id": crawler_id,
            "name": "python.org 채용",
            "interval_minutes": 60,
            "auto_stop_threshold": 3,
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "active"
    assert body["interval_minutes"] == 60
    assert body["auto_stop_threshold"] == 3
    assert body["crawler_status"] == "promoted"
    assert crawler_status(conn, crawler_id) == "promoted"

    row = conn.execute("SELECT * FROM workflows WHERE id = ?", (body["id"],)).fetchone()
    assert row["crawler_id"] == crawler_id
    assert row["name"] == "python.org 채용"
    assert row["interval_minutes"] == 60
    assert row["status"] == "active"
    assert (row["success_count"], row["fail_count"], row["last_run_at"]) == (0, 0, None)


def test_이름을_비우면_크롤러_이름을_쓴다(client: TestClient, conn: sqlite3.Connection) -> None:
    crawler_id = add_crawler(conn, "tested")

    body = client.post("/api/workflows", json={"crawler_id": crawler_id}).json()

    assert body["name"] == "python.org"
    # 기본 주기는 workflows 테이블 기본값과 같다
    assert body["interval_minutes"] == 360
    assert body["auto_stop_threshold"] is None


def test_draft_크롤러의_승격은_거부한다(client: TestClient, conn: sqlite3.Connection) -> None:
    crawler_id = add_crawler(conn, "draft")

    response = client.post("/api/workflows", json={"crawler_id": crawler_id})

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "not_tested"
    assert crawler_status(conn, crawler_id) == "draft"
    assert conn.execute("SELECT count(*) AS n FROM workflows").fetchone()["n"] == 0


def test_이미_승격된_크롤러는_다시_승격하지_않는다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    crawler_id = add_crawler(conn, "promoted")

    response = client.post("/api/workflows", json={"crawler_id": crawler_id})

    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "not_tested"
    assert conn.execute("SELECT count(*) AS n FROM workflows").fetchone()["n"] == 0


def test_없는_크롤러는_404_다(client: TestClient) -> None:
    assert client.post("/api/workflows", json={"crawler_id": 999}).status_code == 404


def test_주기는_1분_미만을_받지_않는다(client: TestClient, conn: sqlite3.Connection) -> None:
    crawler_id = add_crawler(conn, "tested")

    response = client.post("/api/workflows", json={"crawler_id": crawler_id, "interval_minutes": 0})

    assert response.status_code == 422
    assert crawler_status(conn, crawler_id) == "tested"


def promote(client: TestClient, crawler_id: int, minutes: int = 60) -> int:
    response = client.post(
        "/api/workflows", json={"crawler_id": crawler_id, "interval_minutes": minutes}
    )
    assert response.status_code == 201
    return int(response.json()["id"])


def test_승격하면_바로_잡이_생긴다(
    client: TestClient, conn: sqlite3.Connection, scheduler: WorkflowScheduler
) -> None:
    """다음 기동까지 기다리면 그동안 그 워크플로우는 한 번도 돌지 않는다."""
    workflow_id = promote(client, add_crawler(conn, "tested"), minutes=30)

    assert in_round(conn, scheduler) == [workflow_id]


def test_목록은_대상과_주기와_누적값을_준다(client: TestClient, conn: sqlite3.Connection) -> None:
    workflow_id = promote(client, add_crawler(conn, "tested"), minutes=45)
    conn.execute(
        """
        UPDATE workflows
           SET success_count = 4, fail_count = 1, last_run_at = '2026-08-21 10:00:00'
         WHERE id = ?
        """,
        (workflow_id,),
    )
    conn.execute(
        "INSERT INTO crawl_runs (workflow_id, status) VALUES (?, 'success')", (workflow_id,)
    )
    conn.execute(
        "INSERT INTO crawl_runs (workflow_id, status) VALUES (?, 'failed')", (workflow_id,)
    )

    body = client.get("/api/workflows").json()

    assert len(body) == 1
    item = body[0]
    assert item["id"] == workflow_id
    assert item["name"] == "python.org"
    assert item["list_url"] == LIST_URL
    assert item["interval_minutes"] == 45
    assert item["status"] == "active"
    assert item["last_run_at"] == "2026-08-21 10:00:00"
    # 최근 실행은 마지막으로 끝난 실행이다
    assert item["last_run_status"] == "failed"
    assert (item["success_count"], item["fail_count"]) == (4, 1)


def test_실행한_적_없는_워크플로우는_최근_실행이_비어_있다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    promote(client, add_crawler(conn, "tested"))

    item = client.get("/api/workflows").json()[0]

    assert item["last_run_at"] is None
    assert item["last_run_status"] is None


def test_주기를_바꿔도_매일_한_바퀴에_남는다(
    client: TestClient, conn: sqlite3.Connection, scheduler: WorkflowScheduler
) -> None:
    workflow_id = promote(client, add_crawler(conn, "tested"), minutes=60)
    assert in_round(conn, scheduler) == [workflow_id]

    response = client.patch(f"/api/workflows/{workflow_id}", json={"interval_minutes": 15})

    assert response.status_code == 200
    assert response.json()["interval_minutes"] == 15
    assert workflow(conn, workflow_id)["interval_minutes"] == 15
    assert in_round(conn, scheduler) == [workflow_id]


def test_paused_로_바꾸면_잡이_사라진다(
    client: TestClient, conn: sqlite3.Connection, scheduler: WorkflowScheduler
) -> None:
    workflow_id = promote(client, add_crawler(conn, "tested"))

    response = client.patch(f"/api/workflows/{workflow_id}", json={"status": "paused"})

    assert response.status_code == 200
    assert response.json()["status"] == "paused"
    assert workflow(conn, workflow_id)["status"] == "paused"
    assert in_round(conn, scheduler) == []


def test_다시_active_로_바꾸면_잡이_돌아온다(
    client: TestClient, conn: sqlite3.Connection, scheduler: WorkflowScheduler
) -> None:
    workflow_id = promote(client, add_crawler(conn, "tested"), minutes=20)
    client.patch(f"/api/workflows/{workflow_id}", json={"status": "paused"})
    assert in_round(conn, scheduler) == []

    client.patch(f"/api/workflows/{workflow_id}", json={"status": "active"})

    assert in_round(conn, scheduler) == [workflow_id]


def test_주기와_상태를_한_번에_바꿀_수_있다(
    client: TestClient, conn: sqlite3.Connection, scheduler: WorkflowScheduler
) -> None:
    workflow_id = promote(client, add_crawler(conn, "tested"), minutes=60)
    client.patch(f"/api/workflows/{workflow_id}", json={"status": "paused"})

    body = client.patch(
        f"/api/workflows/{workflow_id}", json={"status": "active", "interval_minutes": 5}
    ).json()

    assert (body["status"], body["interval_minutes"]) == ("active", 5)
    assert in_round(conn, scheduler) == [workflow_id]


def test_바꿀_것이_없는_요청은_거절한다(client: TestClient, conn: sqlite3.Connection) -> None:
    workflow_id = promote(client, add_crawler(conn, "tested"))

    assert client.patch(f"/api/workflows/{workflow_id}", json={}).status_code == 422


def test_모르는_상태값은_거절한다(client: TestClient, conn: sqlite3.Connection) -> None:
    workflow_id = promote(client, add_crawler(conn, "tested"))

    response = client.patch(f"/api/workflows/{workflow_id}", json={"status": "stopped"})

    assert response.status_code == 422
    assert workflow(conn, workflow_id)["status"] == "active"


def test_주기는_1분_미만으로_바꿀_수_없다(
    client: TestClient, conn: sqlite3.Connection, scheduler: WorkflowScheduler
) -> None:
    workflow_id = promote(client, add_crawler(conn, "tested"), minutes=60)

    response = client.patch(f"/api/workflows/{workflow_id}", json={"interval_minutes": 0})

    assert response.status_code == 422
    assert in_round(conn, scheduler) == [workflow_id]


def test_없는_워크플로우_변경은_404_다(client: TestClient) -> None:
    assert client.patch("/api/workflows/999", json={"status": "paused"}).status_code == 404
