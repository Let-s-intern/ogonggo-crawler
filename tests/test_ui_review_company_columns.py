"""검수 화면의 회사 두 열 (3.4.V).

계열사가 섞인 워크플로우 하나와, 회사명을 주지 않는 워크플로우 하나를 넣고 화면 경로로만
연다. 실사이트에 나가지 않는다.

| 확인 | 깨지면 |
|---|---|
| 표에 모회사 열과 자회사 열이 따로 있다 | 두 값이 한 칸에 섞여 계열사를 가를 수 없다 |
| 계열사 두 건이 같은 모회사, 다른 자회사로 나온다 | 칸을 가른 일이 화면에 나타나지 않는다 |
| 자회사가 빈 행은 모회사만 값이 있다 | 빈 자리를 모회사로 메운 옛 동작으로 돌아간다 |
| 회사 조건이 두 칸을 함께 본다 | 모회사를 고르면 계열사 공고가 사라진다 |
| 모달에 모회사가 있고 회사명 출처가 없다 | 지운 열의 이름표가 화면에 남는다 |
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

PARENT = "삼성전자"
AFFILIATES = ("삼성SDS", "삼성전기")
# 목록이 회사명을 주지 않는 사이트. 자회사 칸이 비는 것이 정상이다
LONE_PARENT = "토스"


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    for crawler_id, (name, default_company) in enumerate(
        (("삼성 채용", PARENT), ("토스 채용", LONE_PARENT)), start=1
    ):
        connection.execute(
            """
            INSERT INTO crawlers (id, name, list_url, status, default_company)
            VALUES (?, ?, ?, 'promoted', ?)
            """,
            (crawler_id, name, f"https://{crawler_id}.example.test/", default_company),
        )
        connection.execute(
            "INSERT INTO workflows (id, crawler_id, name) VALUES (?, ?, ?)",
            (crawler_id, crawler_id, name),
        )

    rows: tuple[tuple[int, int, str, str | None], ...] = (
        (1, 1, PARENT, AFFILIATES[0]),
        (2, 1, PARENT, AFFILIATES[1]),
        (3, 2, LONE_PARENT, None),
    )
    for raw_id, workflow_id, parent, company_name in rows:
        source_url = f"https://{workflow_id}.example.test/{raw_id}"
        connection.execute(
            """
            INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)
            VALUES (?, ?, ?, '{}', ?)
            """,
            (raw_id, workflow_id, source_url, f"hash-{raw_id}"),
        )
        connection.execute(
            """
            INSERT INTO normalized_jobs
                   (raw_job_id, parent_company_name, company_name, title, body, source_url)
            VALUES (?, ?, ?, ?, '본문', ?)
            """,
            (raw_id, parent, company_name, f"공고 {raw_id}", source_url),
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


def test_표에_모회사_열과_자회사_열이_따로_있다(client: TestClient) -> None:
    html = client.get("/ui/review").text

    # 머리글이 어느 모회사인지까지 적는다. 회사 화면에도 같은 이름의 칸이 있고 그쪽은
    # 사람이 적는 `companies.parent_name` 이다
    assert ">모회사 (크롤러가 정함)</th>" in html
    assert '<th scope="col">자회사</th>' in html
    # 지운 열의 이름표가 남아 있으면 안 된다
    assert "회사명 출처" not in html


def test_계열사_두_건이_같은_모회사와_다른_자회사로_나온다(client: TestClient) -> None:
    """이 Push 가 화면에서 보이는 자리다. 두 칸이 다르지 않으면 가른 일이 없던 일이 된다."""
    html = client.get("/ui/review", params={"workflow_id": "1"}).text

    for affiliate in AFFILIATES:
        assert affiliate in html
    assert html.count(f">{PARENT}</td>") == 2


def test_자회사가_빈_행은_모회사만_값이_있다(client: TestClient) -> None:
    """옛 동작이면 자회사 칸에 `토스` 가 들어가 있었다."""
    html = client.get("/ui/review", params={"workflow_id": "2"}).text

    # 모회사는 읽기 전용 열이라 `td` 에 바로 적히고, 자회사는 값 칸이라 매크로가 그린다
    assert f">{LONE_PARENT}</td>" in html
    assert ">값 없음</span>" in html
    assert f">{LONE_PARENT}</span>" not in html


def test_회사_조건이_모회사와_자회사를_함께_본다(client: TestClient) -> None:
    """모회사를 고르면 계열사 공고가 전부, 자회사를 고르면 그것만 걸린다."""
    whole_group = client.get("/ui/review", params={"company_name": PARENT}).text
    assert "전체 2건" in whole_group

    one = client.get("/ui/review", params={"company_name": AFFILIATES[0]}).text
    assert "전체 1건" in one

    lone = client.get("/ui/review", params={"company_name": LONE_PARENT}).text
    assert "전체 1건" in lone


def test_회사_조건_목록에_두_칸의_이름이_모두_있다(client: TestClient) -> None:
    """자회사만 모으면 회사명을 주지 않는 사이트가 목록에서 통째로 사라진다."""
    html = client.get("/ui/review/filters").text

    for name in (PARENT, LONE_PARENT, *AFFILIATES):
        assert f'<option value="{name}">{name}</option>' in html


def test_검색어가_모회사에도_걸린다(client: TestClient) -> None:
    """자회사만 보면 `토스` 로 검색했을 때 그 사이트의 공고가 하나도 나오지 않는다."""
    html = client.get("/ui/review", params={"q": LONE_PARENT}).text

    assert "전체 1건" in html


def test_모달이_모회사를_적고_회사명_출처를_적지_않는다(client: TestClient) -> None:
    html = client.get("/ui/review/modal/1").text

    assert "모회사" in html
    assert PARENT in html
    assert AFFILIATES[0] in html
    assert "회사명 출처" not in html
