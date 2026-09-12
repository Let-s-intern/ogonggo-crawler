"""검수 화면의 제안 수락·거절 (11.6).

실사이트에 나가지 않는다. 저장된 행과 `job_field_suggestions` 를 넣고 화면 경로로만 처리한다.

| 확인 | 깨지면 |
|---|---|
| 제안이 있는 칸은 표와 모달에 `제안 있음` 이 낱말로 나온다 | 640건에서 제안을 눈으로 찾아야 한다 |
| 수락하면 `job_field_overrides` 에 들어가고 제안 행이 사라진다 | 확정 값이 바뀌지 않는다 |
| 거절하면 제안 행만 사라지고 보정은 그대로다 | 거절했는데 값이 바뀌거나 되돌아간다 |
| 어느 쪽이든 모달은 닫히지 않는다 | 여러 칸을 오가며 판단할 수 없다 |
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import crawlers as crawlers_api
from app.main import app

LIST_URL = "https://www.python.org/jobs/"


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES ('python.org', ?, 'promoted')",
        (LIST_URL,),
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, 'python.org 채용')")
    connection.execute(
        """
        INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)
        VALUES (7, 1, ?, '{}', 'hash-7')
        """,
        (LIST_URL,),
    )
    connection.execute(
        """
        INSERT INTO normalized_jobs (id, raw_job_id, company, title, body, source_url)
        VALUES (3, 7, '파이썬재단', '백엔드 개발자', '본문', ?)
        """,
        (LIST_URL,),
    )
    connection.execute(
        """
        INSERT INTO job_field_suggestions (raw_job_id, field_name, value, reason)
        VALUES (7, 'company', '파이썬 소프트웨어 재단', '원문 하단 회사명이 다르다')
        """
    )
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


def override_of(conn: sqlite3.Connection, field: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM job_field_overrides WHERE raw_job_id = 7 AND field_name = ?",
        (field,),
    ).fetchone()
    return None if row is None else str(row["value"])


def override_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT count(*) AS n FROM job_field_overrides WHERE raw_job_id = 7"
    ).fetchone()
    return int(row["n"])


def suggestion_of(conn: sqlite3.Connection, field: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM job_field_suggestions WHERE raw_job_id = 7 AND field_name = ?",
        (field,),
    ).fetchone()
    return None if row is None else str(row["value"])


def test_제안이_있는_칸은_표와_모달에_제안_있음이_나온다(client: TestClient) -> None:
    table = client.get("/ui/review").text
    modal = client.get("/ui/review/modal/3").text

    assert "제안 있음" in table
    assert "제안 있음" in modal
    # 제안 값과 이유가 모달에 나온다
    assert "파이썬 소프트웨어 재단" in modal
    assert "원문 하단 회사명이 다르다" in modal
    assert 'hx-post="/ui/review/suggestions/3/company"' in modal


def test_수락하면_보정으로_들어가고_제안이_사라진다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    assert override_count(conn) == 0

    response = client.post("/ui/review/suggestions/3/company", data={"action": "accept"})

    assert response.status_code == 200
    assert override_of(conn, "company") == "파이썬 소프트웨어 재단"
    assert suggestion_of(conn, "company") is None
    assert override_count(conn) == 1
    # 처리한 제안은 다시 뜨지 않는다
    assert "제안 있음" not in response.text
    # 여러 칸을 오가며 판단하는 자리라 모달이 닫히지 않는다
    assert "HX-Trigger-After-Settle" not in response.headers
    assert "수락해 사람 보정으로 저장했다" in response.text


def test_거절하면_제안만_사라지고_보정은_그대로다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    response = client.post("/ui/review/suggestions/3/company", data={"action": "reject"})

    assert response.status_code == 200
    assert override_of(conn, "company") is None
    assert suggestion_of(conn, "company") is None
    assert override_count(conn) == 0
    assert "제안 있음" not in response.text
    assert "HX-Trigger-After-Settle" not in response.headers
    assert "거절했다" in response.text


def test_이미_처리된_제안을_다시_누르면_사유를_적고_닫지_않는다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    client.post("/ui/review/suggestions/3/company", data={"action": "reject"})

    response = client.post("/ui/review/suggestions/3/company", data={"action": "reject"})

    assert "이미 처리됐다" in response.text
    assert "HX-Trigger-After-Settle" not in response.headers


def test_고칠_수_없는_필드나_모르는_처리는_사유를_적는다(client: TestClient) -> None:
    bad_field = client.post("/ui/review/suggestions/3/source_url", data={"action": "accept"}).text
    bad_action = client.post("/ui/review/suggestions/3/company", data={"action": "delete"}).text

    assert "고칠 수 없는 필드다" in bad_field
    assert "알 수 없는 처리다" in bad_action
