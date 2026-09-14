"""검수 화면의 직군·직무 칸 (5.1).

실사이트에 나가지 않는다. 저장된 행을 넣고 화면 경로로만 연다. 이름표는 Spring 과 같은 `직군`/`직무`
다 (2026-09-14 결정). 옛 이름표는 `직무 대분류`/`직무 소분류` 였다.

| 확인 | 깨지면 |
|---|---|
| `직군`/`직무` 머리글이 한 번씩 있다 | 옛 자유 글자 직무 열이 되살아나 두 `직무` 가 헷갈린다 |
| 분류된 건은 값이, 안 된 건은 `값 없음` 이다 | 분류 전과 분류가 빈 것을 구분할 수 없다 |
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

LIST_URL = "https://taxonomy.example.test/"

MAJOR = "IT·개발"
MINOR = "서버·백엔드"

TITLES: dict[int, str] = {
    1: "백엔드 개발자",
    2: "아직 분류 안 된 공고",
}


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES ('직무분류 테스트', ?, 'promoted')",
        (LIST_URL,),
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '직무분류 테스트')")
    major_id = connection.execute(
        "INSERT INTO job_taxonomy (parent_id, name) VALUES (NULL, ?)", (MAJOR,)
    ).lastrowid
    connection.execute(
        "INSERT INTO job_taxonomy (parent_id, name) VALUES (?, ?)", (major_id, MINOR)
    )

    for raw_job_id in TITLES:
        connection.execute(
            """
            INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)
            VALUES (?, 1, ?, '{}', ?)
            """,
            (raw_job_id, f"{LIST_URL}{raw_job_id}/", f"hash-{raw_job_id}"),
        )
    # 1번은 분류가 끝난 건, 2번은 아직 분류를 돌리지 않은 건이다
    connection.execute(
        """
        INSERT INTO normalized_jobs (raw_job_id, company_name, title, source_url, job_field,
        job_role)
        VALUES (1, '예시회사', ?, ?, ?, ?)
        """,
        (TITLES[1], f"{LIST_URL}1/", MAJOR, MINOR),
    )
    connection.execute(
        """
        INSERT INTO normalized_jobs (raw_job_id, company_name, title, source_url)
        VALUES (2, '예시회사', ?, ?)
        """,
        (TITLES[2], f"{LIST_URL}2/"),
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


REVIEW_TABLE = re.compile(r"<caption>검수 대상 공고</caption>.*?</table>", re.DOTALL)
CELL = re.compile(
    r'id="review-cell-(\d+)-(job_field|job_role)"[^>]*>\s*<span[^>]*>([^<]*)</span>', re.DOTALL
)


def _table(client: TestClient, **params: str) -> str:
    found = REVIEW_TABLE.search(client.get("/ui/review", params=params).text)
    assert found is not None, "검수 표가 화면에 없다"
    return found.group(0)


def test_직군_직무_머리글이_있다(client: TestClient) -> None:
    table = _table(client)

    # 제목에서 옮기던 자유 글자 직무 열은 0031 이 지웠다. 그 뒤로 `직무` 는 분류표의 소분류다
    assert table.count(">직군</th>") == 1
    assert table.count(">직무</th>") == 1
    assert "직무 대분류" not in table
    assert "직무 소분류" not in table


def test_분류된_건은_값이_안된_건은_값없음이_나온다(client: TestClient) -> None:
    html = client.get("/ui/review").text
    cells = {(int(m.group(1)), m.group(2)): m.group(3) for m in CELL.finditer(html)}

    assert cells[(1, "job_field")] == MAJOR
    assert cells[(1, "job_role")] == MINOR
    assert cells[(2, "job_field")] == "값 없음"
    assert cells[(2, "job_role")] == "값 없음"
