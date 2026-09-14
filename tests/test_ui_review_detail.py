"""공고 한 건을 여는 모달 (16.2, 30.2).

실사이트에 나가지 않는다. 저장된 행을 넣고 화면 경로로만 연다.

Push 30 에서 조회 화면의 읽기 전용 상세 모달을 검수의 편집 모달로 합쳤다. 한 공고를 두
모달로 보여주면 어느 쪽이 저장된 값인지 화면에서 알 수 없다. 상세에만 있던 값
(`raw_jobs` 번호와 내용 해시)은 편집 모달로 옮겼다.

| 확인 | 깨지면 |
|---|---|
| 표에서 한 건을 여는 입구가 하나다 | 상세와 수정이 갈려 어느 쪽이 최신인지 모른다 |
| 페이지에 표 아래 상세 자리가 없다 | 상세가 열리는 자리가 둘이 된다 |
| 모달이 본문·자격요건을 자르지 않는다 | 긴 본문을 확인하려고 원문 사이트를 다시 연다 |
| 원문 링크가 모달 안에 있다 | 모달을 닫아야 원문으로 갈 수 있다 |
| 수집 건 번호와 내용 해시가 모달에 있다 | 어느 수집 건에서 온 값인지 확인할 자리가 없다 |
| 없는 건을 열면 모달 안에 사유를 적는다 | 빈 모달이 열리고 왜인지 알 수 없다 |
| 옛 주소 `/jobs` 가 `/review` 로 간다 | 북마크와 지난 기록의 링크가 죽는다 |
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
SOURCE_URL = "https://www.python.org/jobs/7788/"
# 표 칸에 들어가지 않는 길이. 모달이 이것을 통째로 보여줘야 한다
LONG_BODY = "본문 시작\n" + ("긴 본문이 여기서 이어진다. " * 60) + "\n본문 끝"
LONG_REQUIREMENTS = "자격요건 시작\n" + ("여러 줄로 이어지는 자격요건이다. " * 40) + "\n자격요건 끝"


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
        (SOURCE_URL,),
    )
    connection.execute(
        """
        INSERT INTO normalized_jobs
               (id, raw_job_id, parent_company_name, company_name, title, body, qualifications,
               source_url)
        VALUES (3, 7, '파이썬재단', '파이썬재단 코리아', '백엔드 개발자', ?, ?, ?)
        """,
        (LONG_BODY, LONG_REQUIREMENTS, SOURCE_URL),
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


def test_표에서_한_건을_여는_입구가_하나다(client: TestClient) -> None:
    """상세와 수정을 갈라 두지 않는다. `수정` 하나가 그 공고를 통째로 연다."""
    html = client.get("/ui/review").text

    assert 'id="review-open-3"' in html
    assert "data-modal-open" in html
    assert 'hx-target="#app-modal-body"' in html
    # 읽기 전용 상세로 가던 옛 입구는 남기지 않는다
    assert 'id="job-open-3"' not in html
    assert "/ui/jobs/3" not in html


def test_페이지에_표_아래_상세_자리가_없다(client: TestClient) -> None:
    """상세가 열리는 자리는 모달 하나다. 표 아래 영역은 남기지 않는다."""
    html = client.get("/review").text

    assert 'id="job-detail"' not in html
    assert "표에서 상세를 누르면 여기에 들어온다" not in html
    assert 'id="app-modal"' in html


def test_모달이_본문과_자격요건을_자르지_않는다(client: TestClient) -> None:
    html = client.get("/ui/review/modal/3").text

    assert "본문 시작" in html and "본문 끝" in html
    assert "자격요건 시작" in html and "자격요건 끝" in html
    # 긴 값은 모달 안에서 스크롤한다. 페이지가 아니라 이 자리가 움직인다
    assert "modal-scroll" in html


def test_원문_링크가_모달_안에_있다(client: TestClient) -> None:
    html = client.get("/ui/review/modal/3").text

    assert f'href="{SOURCE_URL}"' in html
    assert "원문 열기" in html


def test_수집_건_번호와_내용_해시가_모달에_있다(client: TestClient) -> None:
    """읽기 전용 상세에만 있던 값이다. 합치면서 사라지지 않았다."""
    html = client.get("/ui/review/modal/3").text

    assert "raw_jobs 7" in html
    assert "hash-7" in html


def test_없는_건은_모달_안에_사유를_적는다(client: TestClient) -> None:
    html = client.get("/ui/review/modal/999").text

    assert "공고 999" in html
    assert "modal-body" in html


def test_옛_조회_주소는_검수로_보낸다(client: TestClient) -> None:
    """북마크가 죽지 않게 한다. 화면 주소만 옮겼고 제공 API 는 그대로다."""
    moved = client.get("/jobs", follow_redirects=False)

    assert moved.status_code == 307
    assert moved.headers["location"] == "/review"
    # 화면 주소만 옮겼다. 제공 API `/api/jobs` 는 소비 측 계약이라 그대로다
    # (`.claude/docs/api-contract.md`, `tests/test_api_jobs.py`)
