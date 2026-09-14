"""검수 화면 조회 조건의 직무 대분류 (5.2).

실사이트에 나가지 않는다. 저장된 행을 넣고 화면 경로로만 연다. 소분류는 대분류에 종속되므로
이 조건은 대분류 하나만 둔다.

| 확인 | 깨지면 |
|---|---|
| 조회 조건에 `직무 대분류` select 가 있고 켜진 것만 담는다 | 꺼진 대분류가 계속 나온다 |
| `job_field` 로 좁히면 그 값을 가진 건만 나온다 | 대분류로 좁혀 볼 방법이 없다 |
| 꺼진 대분류 값으로도 이미 분류된 건은 조회된다 | 대분류를 끄면 그 값으로 분류된 건이 사라진다 |
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
DISABLED_MAJOR = "꺼진대분류"

TITLES: dict[int, str] = {
    1: "백엔드 개발자",
    2: "아직 분류 안 된 공고",
    3: "꺼진 대분류로 분류된 공고",
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
    connection.execute("INSERT INTO job_taxonomy (parent_id, name) VALUES (NULL, ?)", (MAJOR,))
    # 켜진 목록에는 나오지 않아야 하지만, 이미 이 값으로 분류된 공고는 조회할 수 있어야 한다
    connection.execute(
        "INSERT INTO job_taxonomy (parent_id, name, enabled) VALUES (NULL, ?, 0)",
        (DISABLED_MAJOR,),
    )

    for raw_job_id in TITLES:
        connection.execute(
            """
            INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)
            VALUES (?, 1, ?, '{}', ?)
            """,
            (raw_job_id, f"{LIST_URL}{raw_job_id}/", f"hash-{raw_job_id}"),
        )
    connection.execute(
        """
        INSERT INTO normalized_jobs (raw_job_id, company_name, title, source_url, job_field)
        VALUES (1, '예시회사', ?, ?, ?)
        """,
        (TITLES[1], f"{LIST_URL}1/", MAJOR),
    )
    connection.execute(
        """
        INSERT INTO normalized_jobs (raw_job_id, company_name, title, source_url)
        VALUES (2, '예시회사', ?, ?)
        """,
        (TITLES[2], f"{LIST_URL}2/"),
    )
    connection.execute(
        """
        INSERT INTO normalized_jobs (raw_job_id, company_name, title, source_url, job_field)
        VALUES (3, '예시회사', ?, ?, ?)
        """,
        (TITLES[3], f"{LIST_URL}3/", DISABLED_MAJOR),
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


def _titles(client: TestClient, **params: str) -> set[str]:
    html = client.get("/ui/review", params=params).text
    cells = re.findall(r'id="review-cell-\d+-title".*?<span[^>]*>([^<]+)</span>', html, re.DOTALL)
    return set(cells)


def test_조회_조건에_켜진_대분류만_담긴_select가_있다(client: TestClient) -> None:
    html = client.get("/ui/review/filters").text

    assert 'name="job_field"' in html
    assert f'<option value="{MAJOR}">{MAJOR}</option>' in html
    # 꺼진 대분류는 새로 고를 목록에 없다
    assert DISABLED_MAJOR not in html


def test_대분류로_좁히면_그_값을_가진_건만_나온다(client: TestClient) -> None:
    assert _titles(client, job_field=MAJOR) == {TITLES[1]}


def test_꺼진_대분류_값으로도_이미_분류된_건을_찾을_수_있다(client: TestClient) -> None:
    """조회 조건 select 에는 없지만, 그 값으로 조회하는 요청 자체는 막지 않는다."""
    assert _titles(client, job_field=DISABLED_MAJOR) == {TITLES[3]}


def test_대분류_조건이_없으면_전부_나온다(client: TestClient) -> None:
    assert len(_titles(client)) == 3
