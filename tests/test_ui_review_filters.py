"""공고 목록의 조회 조건 (2026-09-15 에 여섯으로 줄였다).

실사이트에 나가지 않는다. 저장된 행을 넣고 화면 경로로만 조회한다.

이 필터가 정확해야 하는 이유는 지우기가 "지금 필터에 걸린 것" 을 대상으로 하기 때문이다.

| 확인 | 깨지면 |
|---|---|
| 모집 여부가 마감일과 오늘로 갈린다 | 마감된 것만 지우려다 모집 중인 것을 지운다 |
| 마감일이 없거나 날짜가 아닌 값은 `마감일 없음` 이다 | 어느 조건에도 안 걸리는 행이 생긴다 |
| 오공고 전송 여부가 `spring_deliveries` 로 갈린다 | 보낸 것과 안 보낸 것을 가를 수 없다 |
| 검색어가 제목과 회사에 걸린다 | 회사로 좁힐 방법이 없다 |
| 조건 여럿을 함께 걸면 AND 다 | 좁힌 줄 알았는데 넓다 |
| 표에 없는 값은 조건을 걸지 않는다 | 화면이 422 로 죽는다 |
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

# (raw_job_id, workflow_id, company_name, title, recruitment_end_at, sent)
ROWS = (
    (1, 1, "엘지전자", "백엔드 개발자", "2099-12-31", False),
    (2, 1, "엘지화학", "프론트 개발자", "2000-01-01", True),
    (3, 1, "엘지전자", "데이터 엔지니어", None, False),
    (4, 2, "이그잼플", "안드로이드 개발자", "상시채용", False),
    (5, 2, "이그잼플", "iOS 개발자", "2099-01-01", False),
)


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES ('lg', ?, 'promoted')",
        (LIST_URL,),
    )
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES ('example', ?, 'promoted')",
        ("https://example.com/jobs/",),
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, 'LG')")
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (2, 'example')")
    for raw_job_id, workflow_id, company_name, title, recruitment_end_at, sent in ROWS:
        source_url = f"{LIST_URL}{raw_job_id}/"
        connection.execute(
            """
            INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)
            VALUES (?, ?, ?, '{}', ?)
            """,
            (raw_job_id, workflow_id, source_url, f"hash-{raw_job_id}"),
        )
        connection.execute(
            """
            INSERT INTO normalized_jobs (raw_job_id, company_name, title, recruitment_end_at,
                                         source_url)
            VALUES (?, ?, ?, ?, ?)
            """,
            (raw_job_id, company_name, title, recruitment_end_at, source_url),
        )
        if sent:
            connection.execute(
                "INSERT INTO spring_deliveries (source_url, status, sent_at)"
                " VALUES (?, 'sent', datetime('now'))",
                (source_url,),
            )
    # 실패한 전송은 보낸 것이 아니다
    connection.execute(
        "INSERT INTO spring_deliveries (source_url, status, last_error)"
        " VALUES (?, 'failed', '400')",
        (f"{LIST_URL}3/",),
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


TITLE_CELL = re.compile(r'class="job-title[^"]*">([^<]+)</span>')


def titles(client: TestClient, **params: str) -> list[str]:
    """조건에 걸린 공고 제목. 표에 그려진 것만 본다."""
    return TITLE_CELL.findall(client.get("/ui/review", params=params).text)


def total(client: TestClient, **params: str) -> int:
    found = re.search(r"전체 (\d+)건 중", client.get("/ui/review", params=params).text)
    assert found is not None
    return int(found.group(1))


def test_조건이_없으면_전부_나온다(client: TestClient) -> None:
    assert total(client) == 5


def test_모집_여부가_마감일과_오늘로_갈린다(client: TestClient) -> None:
    assert set(titles(client, status="open")) == {"백엔드 개발자", "iOS 개발자"}
    assert titles(client, status="closed") == ["프론트 개발자"]


def test_마감일이_없거나_날짜가_아니면_마감일_없음이다(client: TestClient) -> None:
    assert set(titles(client, status="none")) == {"데이터 엔지니어", "안드로이드 개발자"}
    assert (
        total(client, status="open") + total(client, status="closed") + total(client, status="none")
        == 5
    )


def test_오공고_전송_여부는_보낸_것만_전송함이다(client: TestClient) -> None:
    """실패한 전송(3번)은 보낸 것이 아니다."""
    assert titles(client, delivered="yes") == ["프론트 개발자"]
    assert total(client, delivered="no") == 4


def test_검색어가_제목과_회사에_걸린다(client: TestClient) -> None:
    assert set(titles(client, q="엘지전자")) == {"백엔드 개발자", "데이터 엔지니어"}
    assert titles(client, q="iOS") == ["iOS 개발자"]


def test_조건_여럿을_함께_걸면_AND_다(client: TestClient) -> None:
    assert titles(client, workflow_id="1", status="open") == ["백엔드 개발자"]
    assert total(client, workflow_id="1", status="none") == 1


def test_표에_없는_값은_조건을_걸지_않는다(client: TestClient) -> None:
    """화면이 422 로 죽지 않는다. 표가 갱신되지 않는 것이 제일 나쁜 실패다."""
    assert total(client, status="모르는값", workflow_id="abc", delivered="maybe") == 5


def test_필터_폼에_여섯_조건만_있다(client: TestClient) -> None:
    html = client.get("/ui/review/filters").text

    for name in ("q", "workflow_id", "delivered", "status", "job_field", "dup"):
        assert f'name="{name}"' in html
    for removed in ("company_name", "crawled_from", "normalized_from", "empty", "has_suggestion"):
        assert f'name="{removed}"' not in html
    assert "모집 중" in html and "마감일 없음" in html
    assert "전송함" in html and "전송 안 함" in html
