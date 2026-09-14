"""공고가 사라져도 회사 행은 남는지 본다.

회사 행을 지우는 것은 운영자다 (`.claude/tasks/todo/prd-fields-and-logo.md` 4장). 마지막
공고가 지워졌다고 행이 함께 사라지면 운영자가 올려 둔 로고 주소도 같이 사라지고, 그 파일은
저장소에 남아 아무도 찾지 못한다.

지우는 길은 검수 화면 하나다. 그 화면 경로로 지우고 회사 행이 그대로인지 본다 — 함수를 직접
부르면 화면이 다른 문장을 쓰기 시작해도 이 테스트는 통과한다.

실사이트에 나가지 않는다.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import companies, db
from app.api import crawlers as crawlers_api
from app.main import app
from app.normalize.engine import insert_normalized


def add_job(conn: sqlite3.Connection, company_name: str, seq: int) -> int:
    record = {"title": f"공고 {seq}", "body": "본문", "company_name": company_name}
    cursor = conn.execute(
        """
        INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash)
        VALUES (1, ?, ?, ?)
        """,
        (f"https://x/{seq}", json.dumps(record, ensure_ascii=False), f"hash-{seq}"),
    )
    raw_job_id = int(cursor.lastrowid or 0)
    insert_normalized(conn, raw_job_id, [])
    return raw_job_id


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    """삼성SDS 공고 둘과 삼성전기 공고 하나. 회사 행은 정규화가 만든 것이다."""
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        """
        INSERT INTO crawlers (id, name, list_url, status, default_company)
        VALUES (1, '삼성', 'https://x', 'promoted', '삼성전자')
        """
    )
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, '삼성')")
    add_job(connection, "삼성SDS", 1)
    add_job(connection, "삼성SDS", 2)
    add_job(connection, "삼성전기", 3)
    companies.set_logo_url(connection, "삼성SDS", "https://cdn.test/sds.png")
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


def test_the_company_row_outlives_its_last_posting(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """4.4.V. 그 회사의 공고를 다 지워도 회사와 로고는 남는다."""
    response = client.post("/ui/review/delete", data={"raw_job_id": ["1", "2"]})

    assert response.status_code == 200
    assert conn.execute("SELECT count(*) AS n FROM raw_jobs").fetchone()["n"] == 1

    stored = companies.read(conn, "삼성SDS")
    assert stored is not None
    assert stored.logo_url == "https://cdn.test/sds.png"


def test_deleting_every_posting_leaves_the_whole_company_list(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """공고가 하나도 없는 서버에도 회사 목록은 그대로다. 지우는 것은 운영자가 한다."""
    client.post("/ui/review/delete", data={"raw_job_id": ["1", "2", "3"]})

    assert conn.execute("SELECT count(*) AS n FROM normalized_jobs").fetchone()["n"] == 0
    # 모회사(삼성전자)도 자기 행을 갖는다 (2026-08-29 결정, `app/companies.py` 의 `register`)
    assert [company.name for company in companies.list_all(conn)] == [
        "삼성SDS",
        "삼성전기",
        "삼성전자",
    ]


def test_no_code_path_deletes_a_company_row() -> None:
    """지우는 문장이 코드에 없는 것이 4.4 의 구현이다. 생기면 여기서 걸린다."""
    source = pathlib.Path(__file__).resolve().parent.parent / "app"
    offenders = [
        str(path.relative_to(source))
        for path in sorted(source.rglob("*.py"))
        if "DELETE FROM companies" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
