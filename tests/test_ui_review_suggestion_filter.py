"""검수 화면의 제안 여부 조회 조건 (11.7).

실사이트에 나가지 않는다. 저장된 행과 `job_field_suggestions` 를 넣고 화면 경로로만 조회한다.

640건에서 제안이 붙은 것을 눈으로 찾게 두면 아무도 수락하지 않는다 — 조건으로 걸러 봐야
한다.

| 확인 | 깨지면 |
|---|---|
| `has_suggestion=yes` 는 제안이 붙은 건만 나온다 | 640건을 눈으로 훑어야 한다 |
| `has_suggestion=no` 는 제안이 없는 건만 나온다 | 이미 처리한 건도 계속 걸린다 |
| 건수도 표에 걸린 것과 같다 | 화면에 적힌 건수가 거짓이 된다 |
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

LIST_URL = "https://www.python.org/jobs/"

# 제안이 있는 두 건(1, 2)과 없는 세 건(3, 4, 5)
TITLES: dict[int, str] = {
    1: "백엔드 개발자",
    2: "프론트 개발자",
    3: "데이터 엔지니어",
    4: "안드로이드 개발자",
    5: "iOS 개발자",
}
WITH_SUGGESTION = (1, 2)
WITHOUT_SUGGESTION = (3, 4, 5)


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES ('python.org', ?, 'promoted')",
        (LIST_URL,),
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, 'python.org 채용')")
    for raw_job_id, title in TITLES.items():
        connection.execute(
            """
            INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)
            VALUES (?, 1, ?, '{}', ?)
            """,
            (raw_job_id, f"{LIST_URL}{raw_job_id}/", f"hash-{raw_job_id}"),
        )
        connection.execute(
            """
            INSERT INTO normalized_jobs (raw_job_id, company_name, title, source_url)
            VALUES (?, '파이썬재단', ?, ?)
            """,
            (raw_job_id, title, f"{LIST_URL}{raw_job_id}/"),
        )
    for raw_job_id in WITH_SUGGESTION:
        connection.execute(
            """
            INSERT INTO job_field_suggestions (raw_job_id, field_name, value, reason)
            VALUES (?, 'company_name', '다른 회사명', '원문과 다르다')
            """,
            (raw_job_id,),
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


TITLE_CELL = re.compile(r'id="review-cell-\d+-title".*?<span[^>]*>([^<]+)</span>', re.DOTALL)


def titles(client: TestClient, **params: str) -> set[str]:
    html = client.get("/ui/review", params=params).text
    return set(TITLE_CELL.findall(html))


def total(client: TestClient, **params: str) -> int:
    html = client.get("/ui/review", params=params).text
    found = re.search(r"(\d+)건 중", html)
    assert found is not None, html[:400]
    return int(found.group(1))


def test_조건이_없으면_전부_나온다(client: TestClient) -> None:
    assert total(client) == 5


def test_제안이_있는_건만_나온다(client: TestClient) -> None:
    assert titles(client, has_suggestion="yes") == {TITLES[i] for i in WITH_SUGGESTION}
    assert total(client, has_suggestion="yes") == 2


def test_제안이_없는_건만_나온다(client: TestClient) -> None:
    assert titles(client, has_suggestion="no") == {TITLES[i] for i in WITHOUT_SUGGESTION}
    assert total(client, has_suggestion="no") == 3


def test_조건을_모르는_값이면_안_걸린다(client: TestClient) -> None:
    assert total(client, has_suggestion="maybe") == 5


def test_필터가_화면에_라디오나_셀렉트로_나온다(client: TestClient) -> None:
    html = client.get("/ui/review/filters").text

    assert 'name="has_suggestion"' in html
    assert "제안 있음" in html
    assert "제안 없음" in html
