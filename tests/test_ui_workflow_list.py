"""워크플로우 목록 화면 테스트 (15.2).

실사이트에 나가지 않는다. 목록·주기 변경·중지·재개는 DB 와 스케줄러 잡만 만진다.

| 확인 | 깨지면 |
|---|---|
| 빈 목록이 승격 경로를 가리킨다 | 화면이 "API 로만 승격된다" 는 지난 사실을 계속 적는다 |
| 승격한 워크플로우가 이름·대상·주기·상태·누적으로 나온다 | 승격했는데 어디에도 안 보인다 |
| 조작이 누른 카드 하나만 돌려준다 | 목록이 다시 그려지며 다른 카드에 입력하던 주기가 날아간다 |
"""

from __future__ import annotations

import pathlib
import re
import sqlite3
from collections.abc import Iterator

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi.testclient import TestClient

from app import db
from app.api import crawlers as crawlers_api
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
    app.dependency_overrides[crawlers_api.get_connection] = request_connection
    app.dependency_overrides[workflows_api.get_workflow_scheduler] = lambda: scheduler
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def promote(client: TestClient, conn: sqlite3.Connection, name: str, minutes: int) -> int:
    """화면 경로로 승격한다. 이 화면에 행이 오는 경로가 그것 하나여야 한다."""
    cursor = conn.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES (?, ?, 'tested')",
        (name, LIST_URL),
    )
    crawler_id = int(cursor.lastrowid or 0)
    client.post(
        "/ui/workflows",
        data={
            "crawler_id": str(crawler_id),
            "name": name,
            "interval_minutes": str(minutes),
        },
    )
    row = conn.execute("SELECT id FROM workflows WHERE crawler_id = ?", (crawler_id,)).fetchone()
    return int(row["id"])


def rows_in(html: str) -> list[str]:
    """돌려준 조각에 들어 있는 워크플로우 카드. 하나여야 그 카드만 갈린 것이다."""
    return re.findall(r'<article id="workflow-row-(\d+)"', html)


def test_빈_목록은_승격_화면을_가리킨다(client: TestClient) -> None:
    """승격이 화면에 생겼다. API 로만 된다는 안내는 더 이상 사실이 아니다."""
    html = client.get("/ui/workflows").text

    assert "등록된 워크플로우가 없다" in html
    assert "POST /api/workflows" not in html
    assert 'href="/tests"' in html


def test_승격한_워크플로우가_목록에_나온다(client: TestClient, conn: sqlite3.Connection) -> None:
    workflow_id = promote(client, conn, "python.org 채용", 120)

    html = client.get("/ui/workflows").text

    assert rows_in(html) == [str(workflow_id)]
    assert "python.org 채용" in html  # 이름
    assert LIST_URL in html  # 대상 사이트
    assert "매일 08:30부터 한 곳씩" in html  # 언제 도는가
    assert "실행 중" in html  # 상태 (active)
    assert "실행 기록 없음" in html  # 최근 실행
    assert "임계치 없음" in html


def test_주기_변경은_누른_카드만_돌려준다(client: TestClient, conn: sqlite3.Connection) -> None:
    first = promote(client, conn, "첫 워크플로우", 120)
    second = promote(client, conn, "둘째 워크플로우", 360)

    html = client.patch(f"/ui/workflows/{first}", data={"interval_minutes": "30"}).text

    assert rows_in(html) == [str(first)]
    assert str(second) not in rows_in(html)
    assert "주기를 30분으로 바꿨다" in html
    row = conn.execute("SELECT interval_minutes FROM workflows WHERE id = ?", (first,)).fetchone()
    assert row["interval_minutes"] == 30


def test_중지와_재개도_누른_카드만_돌려준다(
    client: TestClient, conn: sqlite3.Connection, scheduler: WorkflowScheduler
) -> None:
    first = promote(client, conn, "첫 워크플로우", 120)
    second = promote(client, conn, "둘째 워크플로우", 360)

    paused = client.patch(f"/ui/workflows/{first}", data={"status": "paused"}).text
    assert rows_in(paused) == [str(first)]
    assert "중지했다" in paused
    assert "중지됨" in paused
    # 잡까지 가야 실제로 멈춘다. 둘째는 그대로 남는다
    assert in_round(conn, scheduler) == [second]

    resumed = client.patch(f"/ui/workflows/{first}", data={"status": "active"}).text
    assert rows_in(resumed) == [str(first)]
    assert "재개했다" in resumed
    assert in_round(conn, scheduler) == [first, second]
