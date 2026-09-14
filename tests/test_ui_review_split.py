"""검수 화면이 나눈 공고를 하나씩 가리키는지 본다 (2026-09-11 결정).

나눈 공고는 수집 건 하나에 정규화 행이 여럿이다. 화면은 공고 번호(`normalized_jobs.id`)로
가리키고, 보정·제안은 (수집 건, 번호) 로 저장한다. 수집 건으로 가리키면 한 직무를 고쳤는데
다른 직무가 열리거나 바뀐다. 실사이트에 나가지 않는다.
"""

from __future__ import annotations

import pathlib
import re
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import crawlers as crawlers_api
from app.main import app

URL = "https://example.test/jobs/1"
TITLE = "2026년 하반기 R&D 경력사원 채용"


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES ('예시', ?, 'promoted')", (URL,)
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '예시 채용')")
    connection.execute(
        "INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)"
        " VALUES (7, 1, ?, '{}', 'hash-7')",
        (URL,),
    )
    connection.executemany(
        "INSERT INTO normalized_jobs (id, raw_job_id, part, company_name, title, body, source_url)"
        " VALUES (?, 7, ?, '예시회사', ?, '본문', ?)",
        [
            (1, 1, f"{TITLE} - 로봇 SW 개발", f"{URL}#1"),
            (2, 2, f"{TITLE} - 비전 AI 연구", f"{URL}#2"),
        ],
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


def suggest(conn: sqlite3.Connection, part: int, value: str) -> None:
    conn.execute(
        "INSERT INTO job_field_suggestions (raw_job_id, part, field_name, value, reason)"
        " VALUES (7, ?, 'company_name', ?, '원문 하단 회사명이 다르다')",
        (part, value),
    )


def rows(conn: sqlite3.Connection, table: str) -> list[tuple[object, ...]]:
    found = conn.execute(
        f"SELECT part, field_name, value FROM {table} WHERE raw_job_id = 7 ORDER BY part"
    ).fetchall()
    return [tuple(row) for row in found]


def block(html: str, element_id: str) -> str:
    """그 id 를 가진 `div` 안쪽 글자."""
    found = re.search(rf'<div id="{element_id}"[^>]*>(.*?)</div>', html, re.S)
    assert found is not None, f"{element_id} 가 화면에 없다"
    return found.group(1)


def test_표의_칸과_버튼은_공고마다_따로다(client: TestClient) -> None:
    """같은 수집 건의 두 행이 같은 id 를 가지면 저장 뒤 엉뚱한 행이 갈린다."""
    table = client.get("/ui/review").text

    assert 'id="review-cell-1-title"' in table
    assert 'id="review-cell-2-title"' in table
    assert 'hx-get="/ui/review/modal/1"' in table
    assert 'hx-get="/ui/review/modal/2"' in table


def test_모달은_그_공고를_연다(client: TestClient) -> None:
    modal = client.get("/ui/review/modal/2").text

    assert "비전 AI 연구" in modal
    assert "로봇 SW 개발" not in modal
    assert 'hx-put="/ui/review/jobs/2"' in modal


def test_저장한_보정은_그_번호의_공고에만_붙는다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    response = client.put("/ui/review/jobs/2", data={"title": "사람이 정한 제목"})

    assert response.status_code == 200
    assert rows(conn, "job_field_overrides") == [(2, "title", "사람이 정한 제목")]
    table = client.get("/ui/review").text
    assert "없음" in block(table, "review-override-count-1")
    assert "1개 필드" in block(table, "review-override-count-2")


def test_제안_수락은_그_번호의_제안만_처리한다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    suggest(conn, 1, "첫째 회사")
    suggest(conn, 2, "둘째 회사")

    client.post("/ui/review/suggestions/2/company_name", data={"action": "accept"})

    assert rows(conn, "job_field_overrides") == [(2, "company_name", "둘째 회사")]
    assert rows(conn, "job_field_suggestions") == [(1, "company_name", "첫째 회사")]


def test_제안_있음_조건은_번호마다_본다(client: TestClient, conn: sqlite3.Connection) -> None:
    """한 직무에 붙은 제안으로 형제 공고까지 걸리면 검수할 곳을 찾을 수 없다."""
    suggest(conn, 2, "둘째 회사")

    table = client.get("/ui/review", params={"has_suggestion": "yes"}).text

    assert 'id="review-cell-2-title"' in table
    assert 'id="review-cell-1-title"' not in table


def test_없는_공고_번호는_사유를_적는다(client: TestClient) -> None:
    modal = client.get("/ui/review/modal/99").text

    assert "공고 99 의 정규화 행이 없다" in modal
