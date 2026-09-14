"""부가 워크플로우 실행 API (3.5.V).

모델에도 실사이트에도 나가지 않는다. 가짜 제공자 하나가 첫 호출에서 멈춰 서서, 도는 동안
`GET` 이 무엇을 돌려주는지 볼 수 있게 한다.
"""

from __future__ import annotations

import pathlib
import sqlite3
import threading
import time
from collections.abc import Iterator
from functools import partial
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import rules as rules_api
from app.api import side as side_api
from app.main import app
from app.side import runner, runs, store
from tests.test_classify_run import GOOD, settings_with_key
from tests.test_classify_run import _seed as seed
from tests.test_selector_generator import FakeClient


class BlockingClient(FakeClient):
    """첫 호출에서 문이 열릴 때까지 선다. 도는 실행을 실제로 만들려면 이것이 필요하다."""

    def __init__(self, *texts: str) -> None:
        super().__init__(*texts)
        self.gate = threading.Event()
        self.entered = threading.Event()
        answer = self.models.generate_content

        async def generate_content(**kwargs: Any) -> Any:
            self.entered.set()
            self.gate.wait(10)
            return await answer(**kwargs)

        self.models.generate_content = generate_content  # type: ignore[method-assign]


@pytest.fixture
def path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "jobs.db"


@pytest.fixture
def conn(path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(path)
    db.migrate_up(connection)
    seed(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def workflow(conn: sqlite3.Connection) -> store.SideWorkflow:
    return store.create(conn, kind="classify", name="미분류 분류")


def make_client(path: pathlib.Path, client: Any) -> Iterator[TestClient]:
    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(path)
        try:
            yield connection
        finally:
            connection.close()

    app.dependency_overrides[rules_api.get_connection] = request_connection
    app.dependency_overrides[rules_api.get_connect_factory] = lambda: partial(db.connect, path)
    app.dependency_overrides[side_api.get_start] = lambda: partial(
        runner.start, client=client, settings=settings_with_key()
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def client(path: pathlib.Path, conn: sqlite3.Connection) -> Iterator[TestClient]:
    yield from make_client(path, FakeClient(GOOD, GOOD, GOOD))


def wait_until_done(client: TestClient, workflow_id: int) -> dict[str, Any]:
    """끝날 때까지 폴링한다. 화면이 하는 것과 같은 일이다."""
    recruitment_end_at = time.monotonic() + 15
    while time.monotonic() < recruitment_end_at:
        body: dict[str, Any] = client.get(f"/api/side/{workflow_id}").json()
        if not body["running"]:
            return body
        time.sleep(0.05)
    raise AssertionError("실행이 끝나지 않았다")


def test_starting_a_run_answers_that_it_started(
    client: TestClient, workflow: store.SideWorkflow
) -> None:
    response = client.post(f"/api/side/{workflow.id}/run")

    assert response.status_code == 202
    body = response.json()
    # 응답은 시작했다는 것까지다. 종료는 아직 없다
    assert body["status"] is None
    assert body["running"] is True
    assert body["trigger"] == "manual"

    done = wait_until_done(client, workflow.id)
    assert done["last_run"]["status"] == runs.SUCCESS
    assert done["last_run"]["target_count"] == 3
    assert done["last_run"]["processed_count"] == 3


def test_the_progress_is_visible_while_it_runs(
    path: pathlib.Path, conn: sqlite3.Connection, workflow: store.SideWorkflow
) -> None:
    """도는 동안 GET 이 진행 중을 돌려주고, 끝난 뒤 건수가 맞는다."""
    blocking = BlockingClient(GOOD, GOOD, GOOD)
    for client in make_client(path, blocking):
        assert client.post(f"/api/side/{workflow.id}/run").status_code == 202
        assert blocking.entered.wait(10)

        during = client.get(f"/api/side/{workflow.id}").json()
        assert during["running"] is True
        assert during["last_run"]["status"] is None
        assert during["last_run_at"] is not None

        blocking.gate.set()
        done = wait_until_done(client, workflow.id)

        assert done["running"] is False
        assert done["last_run"]["processed_count"] == 3
        assert done["last_run"]["failed_count"] == 0


def test_a_second_start_while_it_runs_is_refused_and_recorded(
    path: pathlib.Path, conn: sqlite3.Connection, workflow: store.SideWorkflow
) -> None:
    """응답을 받은 사람은 아는데 이력은 모르는 상태를 만들지 않는다."""
    blocking = BlockingClient(GOOD, GOOD, GOOD)
    for client in make_client(path, blocking):
        client.post(f"/api/side/{workflow.id}/run")
        assert blocking.entered.wait(10)

        second = client.post(f"/api/side/{workflow.id}/run")

        assert second.status_code == 409
        assert second.json()["detail"]["reason"] == "already_running"
        blocking.gate.set()
        wait_until_done(client, workflow.id)

    assert conn.execute("SELECT count(*) AS n FROM side_runs").fetchone()["n"] == 2
    skipped = runs.latest(conn, workflow.id)
    assert skipped is not None and skipped.status == runs.SKIPPED


def test_reading_a_workflow_that_never_ran(
    client: TestClient, workflow: store.SideWorkflow
) -> None:
    body = client.get(f"/api/side/{workflow.id}").json()

    assert body["running"] is False
    assert body["last_run"] is None
    assert body["target_scope"] == "unclassified"
    assert body["batch_limit"] == 50


def test_a_workflow_that_is_not_there(client: TestClient) -> None:
    assert client.get("/api/side/404").status_code == 404
    assert client.post("/api/side/404/run").status_code == 404
