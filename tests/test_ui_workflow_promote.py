"""화면에서의 워크플로우 승격 테스트 (15.1).

실사이트에 나가지 않는다. 승격은 DB 와 스케줄러 잡만 만지므로 fetch 클라이언트가 필요 없다.

확인하는 것은 셋이다.

| 확인 | 깨지면 |
|---|---|
| `tested` 에만 승격 수단이 있다 | 눌러도 409 로 거절되는 버튼이 화면에 남는다 |
| 승격이 `workflows` 행과 크롤러 상태를 바꾼다 | 화면이 승격했다고 적고 아무 일도 안 일어난다 |
| 스케줄러 잡까지 간다 | 승격한 워크플로우가 다음 기동 전까지 한 번도 안 돈다 |

승격 로직은 `app/api/workflows.py` 것을 그대로 부른다. 여기서 확인하는 것은 화면이 그 경로를
쓰는가이지 승격 규칙 자체가 아니다 — 규칙은 `tests/test_api_workflows.py` 가 본다.
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


def add_crawler(conn: sqlite3.Connection, status: str, name: str = "python.org") -> int:
    cursor = conn.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES (?, ?, ?)",
        (name, LIST_URL, status),
    )
    return int(cursor.lastrowid or 0)


def caption(html: str) -> str:
    found = re.search(r"<caption>(.*?)</caption>", html, re.S)
    assert found is not None
    return found.group(1)


def test_tested_크롤러에만_승격_수단이_있다(client: TestClient, conn: sqlite3.Connection) -> None:
    draft_id = add_crawler(conn, "draft", name="초안 크롤러")
    tested_id = add_crawler(conn, "tested", name="테스트된 크롤러")

    html = client.get("/ui/test-targets").text

    assert f'name="crawler_id" value="{tested_id}"' in html
    assert f'name="crawler_id" value="{draft_id}"' not in html
    assert html.count(">워크플로우로 승격</button>") == 1


def test_승격_줄은_열을_늘리지_않는다(client: TestClient, conn: sqlite3.Connection) -> None:
    """열을 하나 더하면 이 표는 가로로 넘쳐 승격 버튼이 스크롤 밖으로 나간다."""
    add_crawler(conn, "tested")

    html = client.get("/ui/test-targets").text

    header = re.search(r"<thead>(.*?)</thead>", html, re.S)
    assert header is not None
    assert header.group(1).count("<th") == 6
    assert 'colspan="6"' in html


def test_승격하면_행이_생기고_크롤러가_promoted_가_된다(
    client: TestClient, conn: sqlite3.Connection, scheduler: WorkflowScheduler
) -> None:
    crawler_id = add_crawler(conn, "tested")

    response = client.post(
        "/ui/workflows",
        data={"crawler_id": str(crawler_id), "name": "python.org 채용", "interval_minutes": "120"},
    )

    assert response.status_code == 200
    row = conn.execute("SELECT * FROM workflows WHERE crawler_id = ?", (crawler_id,)).fetchone()
    assert row["name"] == "python.org 채용"
    assert row["interval_minutes"] == 120
    assert row["status"] == "active"

    status = conn.execute("SELECT status FROM crawlers WHERE id = ?", (crawler_id,)).fetchone()[
        "status"
    ]
    assert status == "promoted"
    # 승격은 곧 active 다. 잡까지 가지 않으면 다음 기동 전까지 한 번도 돌지 않는다
    assert in_round(conn, scheduler) == [int(row["id"])]


def test_승격_결과와_워크플로우_화면으로_가는_수단이_같이_온다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    crawler_id = add_crawler(conn, "tested")

    html = client.post("/ui/workflows", data={"crawler_id": str(crawler_id)}).text

    text = caption(html)
    assert "승격했다" in text
    assert 'href="/workflows"' in text
    # 승격한 크롤러의 자리에는 승격 폼 대신 상태가 남는다
    assert f'name="crawler_id" value="{crawler_id}"' not in html


def test_주기를_비우면_기본값_360_분이다(client: TestClient, conn: sqlite3.Connection) -> None:
    crawler_id = add_crawler(conn, "tested")

    client.post("/ui/workflows", data={"crawler_id": str(crawler_id), "interval_minutes": ""})

    row = conn.execute(
        "SELECT interval_minutes FROM workflows WHERE crawler_id = ?", (crawler_id,)
    ).fetchone()
    assert row["interval_minutes"] == 360


def test_승격_폼에_주기_칸이_없다(client: TestClient, conn: sqlite3.Connection) -> None:
    """사이트마다 주기를 고르지 않는다. 승격하면 매일 한 바퀴에 들어간다."""
    crawler_id = add_crawler(conn, "tested")

    html = client.get("/ui/test-targets").text

    assert f'id="wf-interval-{crawler_id}"' not in html


def test_draft_크롤러는_승격되지_않고_사유가_뜬다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """화면에 버튼이 없어도 요청은 올 수 있다. 거절은 API 가 하고 화면은 사유를 적는다."""
    crawler_id = add_crawler(conn, "draft")

    html = client.post("/ui/workflows", data={"crawler_id": str(crawler_id)}).text

    assert "승격하지 못했다" in caption(html)
    assert "tested" in caption(html)
    assert conn.execute("SELECT COUNT(*) AS n FROM workflows").fetchone()["n"] == 0


def test_주기가_숫자가_아니면_승격하지_않는다(client: TestClient, conn: sqlite3.Connection) -> None:
    crawler_id = add_crawler(conn, "tested")

    html = client.post(
        "/ui/workflows", data={"crawler_id": str(crawler_id), "interval_minutes": "0"}
    ).text

    assert "주기는 1 이상의 정수여야 한다" in caption(html)
    assert conn.execute("SELECT COUNT(*) AS n FROM workflows").fetchone()["n"] == 0
    # 거절해도 표는 그대로 있다. 승격 수단이 사라지면 고쳐서 다시 보낼 자리가 없다
    assert f'name="crawler_id" value="{crawler_id}"' in html


def test_승격_대기_문구는_표_안에_있다(client: TestClient, conn: sqlite3.Connection) -> None:
    """`.data-table .wait-note` 가 흐름에서 빼낸다. 표 밖에 두면 대기 표시가 표를 민다."""
    add_crawler(conn, "tested")

    html = client.get("/ui/test-targets").text

    table = html[html.index("<table") : html.index("</table>")]
    assert html.count("wait-note") == table.count("wait-note")
    # 저장 모드 전환, 테스트 실행, 승격
    assert table.count("wait-note") == 3
