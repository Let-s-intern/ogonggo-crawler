"""데이터 검수 화면의 조회 조건 (27.5, 30.2).

실사이트에 나가지 않는다. 저장된 행을 넣고 화면 경로로만 조회한다.

이 필터가 정확해야 하는 이유는 지우기가 "지금 필터에 걸린 것" 을 대상으로 하기 때문이다.
조건이 한 건이라도 어긋나면 지울 생각이 없던 행이 확인 창의 건수 안에 들어간다.

| 확인 | 깨지면 |
|---|---|
| 진행 여부가 마감일과 오늘로 갈린다 | 마감된 것만 지우려다 진행중인 것을 지운다 |
| 마감일이 없거나 날짜가 아닌 값은 `마감일 없음` 이다 | 어느 조건에도 안 걸리는 행이 생긴다 |
| 전달 여부가 `delivered_at` 으로 갈린다 | 이미 보낸 것을 골라낼 수 없다 |
| 시각 범위가 표시 시간대의 하루로 걸린다 | 자정 근처 아홉 시간이 반대쪽 날에 걸린다 |
| 끝나는 날이 그날을 포함한다 | 마지막 날 수집분이 조건에서 빠진다 |
| 조건 여럿을 함께 걸면 AND 다 | 좁힌 줄 알았는데 넓다 |
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

# (raw_job_id, workflow_id, company_name, title, recruitment_end_at, crawled_at, normalized_at,
# delivered)
ROWS = (
    (
        1,
        1,
        "엘지전자",
        "백엔드 개발자",
        "2099-12-31",
        "2026-08-20 01:00:00",
        "2026-08-20 02:00:00",
        False,
    ),
    (
        2,
        1,
        "엘지화학",
        "프론트 개발자",
        "2000-01-01",
        "2026-08-21 01:00:00",
        "2026-08-21 02:00:00",
        True,
    ),
    (
        3,
        1,
        "엘지전자",
        "데이터 엔지니어",
        None,
        "2026-08-22 01:00:00",
        "2026-08-22 02:00:00",
        False,
    ),
    (
        4,
        2,
        "이그잼플",
        "안드로이드 개발자",
        "상시채용",
        "2026-08-23 01:00:00",
        "2026-08-23 02:00:00",
        False,
    ),
    # 표시 시간대(KST)로는 2026-08-25 00:30 이고 UTC 로는 2026-08-24 15:30 이다.
    # 날짜 문자열을 그대로 비교하면 이 행이 24일에 걸린다
    (
        5,
        2,
        "이그잼플",
        "iOS 개발자",
        "2099-01-01",
        "2026-08-24 15:30:00",
        "2026-08-24 15:30:00",
        False,
    ),
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
    for (
        raw_job_id,
        workflow_id,
        company_name,
        title,
        recruitment_end_at,
        crawled,
        normalized,
        sent,
    ) in ROWS:
        connection.execute(
            """
            INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash,
                                  crawled_at)
            VALUES (?, ?, ?, '{}', ?, ?)
            """,
            (raw_job_id, workflow_id, f"{LIST_URL}{raw_job_id}/", f"hash-{raw_job_id}", crawled),
        )
        connection.execute(
            """
            INSERT INTO normalized_jobs (raw_job_id, company_name, title, recruitment_end_at,
            source_url,
                                         normalized_at, delivered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                raw_job_id,
                company_name,
                title,
                recruitment_end_at,
                f"{LIST_URL}{raw_job_id}/",
                normalized,
                "2026-08-22 03:00:00" if sent else None,
            ),
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


# 표의 제목 칸. 값 칸은 보정 여부까지 함께 그리는 매크로가 만든다
# (`fragments/review_cell_macro.html`)
TITLE_CELL = re.compile(r'id="review-cell-\d+-title".*?<span[^>]*>([^<]+)</span>', re.DOTALL)


def titles(client: TestClient, **params: str) -> list[str]:
    """조건에 걸린 공고 제목. 표에 그려진 것만 본다."""
    html = client.get("/ui/review", params=params).text
    return TITLE_CELL.findall(html)


def total(client: TestClient, **params: str) -> int:
    html = client.get("/ui/review", params=params).text
    found = re.search(r"(\d+)건 중", html)
    assert found is not None, html[:400]
    return int(found.group(1))


def test_조건이_없으면_전부_나온다(client: TestClient) -> None:
    assert total(client) == 5


def test_진행_여부가_마감일과_오늘로_갈린다(client: TestClient) -> None:
    assert set(titles(client, status="open")) == {"백엔드 개발자", "iOS 개발자"}
    assert titles(client, status="closed") == ["프론트 개발자"]


def test_마감일이_없거나_날짜가_아니면_마감일_없음이다(client: TestClient) -> None:
    """`상시채용` 처럼 날짜로 읽히지 않는 값도 여기 모인다.

    어느 조건에도 걸리지 않는 행을 남기지 않는다.
    """
    assert set(titles(client, status="none")) == {"데이터 엔지니어", "안드로이드 개발자"}
    # 세 갈래를 합치면 전부다
    assert (
        total(client, status="open") + total(client, status="closed") + total(client, status="none")
        == 5
    )


def test_전달_여부가_delivered_at_으로_갈린다(client: TestClient) -> None:
    assert titles(client, delivered="yes") == ["프론트 개발자"]
    assert total(client, delivered="no") == 4


def test_수집_시각_범위가_표시_시간대의_하루로_걸린다(client: TestClient) -> None:
    """UTC 2026-08-24 15:30 은 KST 로 2026-08-25 다. 화면에 25일로 보이는 행이 25일에 걸린다."""
    assert titles(client, crawled_from="2026-08-25") == ["iOS 개발자"]
    assert titles(client, crawled_to="2026-08-20") == ["백엔드 개발자"]
    assert total(client, crawled_from="2026-08-21", crawled_to="2026-08-23") == 3


def test_끝나는_날이_그날을_포함한다(client: TestClient) -> None:
    assert total(client, crawled_from="2026-08-20", crawled_to="2026-08-20") == 1


def test_정규화_시각_범위도_따로_걸린다(client: TestClient) -> None:
    assert total(client, normalized_from="2026-08-22", normalized_to="2026-08-22") == 1


def test_조건_여럿을_함께_걸면_AND_다(client: TestClient) -> None:
    assert titles(client, workflow_id="1", company_name="엘지전자", status="open") == [
        "백엔드 개발자"
    ]
    assert total(client, workflow_id="1", status="none") == 1


def test_읽지_못하는_날짜는_조건을_걸지_않는다(client: TestClient) -> None:
    """화면이 422 로 죽지 않는다. 표가 갱신되지 않는 것이 제일 나쁜 실패다."""
    assert total(client, crawled_from="어제") == 5


def test_필터_폼에_새_조건이_모두_있다(client: TestClient) -> None:
    html = client.get("/ui/review/filters").text

    for name in (
        "workflow_id",
        "company_name",
        "status",
        "delivered",
        "crawled_from",
        "crawled_to",
        "normalized_from",
        "normalized_to",
        "q",
    ):
        assert f'name="{name}"' in html
    assert "진행중" in html and "마감 지남" in html and "마감일 없음" in html
    assert "전달됨" in html and "미전달" in html
