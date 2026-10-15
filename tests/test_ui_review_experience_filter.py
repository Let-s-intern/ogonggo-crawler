"""공고 목록 조회 조건의 경력 구분.

실사이트에 나가지 않는다. 저장된 행을 넣고 화면 경로로만 연다. 값은 분류가 고르는 경력 구분
(`app/classify/schema.py` 의 `EXPERIENCE_TYPES`) 그대로다.

| 확인 | 깨지면 |
|---|---|
| 조회 조건에 `경력` select 가 있다 | 경력으로 좁혀 볼 방법이 없다 |
| `experience` 로 좁히면 그 값을 가진 건만 나온다 | 신입 공고만 골라 보낼 수 없다 |
| 다른 조건(보냄·마감일 없음)과 겹쳐 걸린다 | 이미 보낸 공고 중 경력별로 마감일을 못 고친다 |
| 목록에 없는 값은 조건을 걸지 않은 것으로 본다 | 엉뚱한 값 하나로 표가 비거나 오류가 난다 |
| `3년 이하` 는 자동 전송과 같은 연차 조건이다 | 화면에서 고른 범위와 자동 전송 범위가 어긋난다 |
"""

from __future__ import annotations

import pathlib
import re
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import MultiDict

from app import db
from app.api import crawlers as crawlers_api
from app.api.review_filter import form_raw_ids
from app.main import app

LIST_URL = "https://experience.example.test/"

# 번호, 제목, 경력 구분, 마감일, 오공고로 보냈는가
ROWS: tuple[tuple[int, str, str | None, str | None, bool], ...] = (
    (1, "신입 마케터", "NEWCOMER", None, True),
    (2, "경력 마케터", "EXPERIENCED", None, True),
    (3, "신입 개발자", "NEWCOMER", "2099-12-31 23:59:00", True),
    (4, "보내지 않은 신입", "NEWCOMER", None, False),
    (5, "아직 분류 안 된 공고", None, None, False),
)


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES ('경력 테스트', ?, 'promoted')",
        (LIST_URL,),
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '경력 테스트')")
    for raw_job_id, title, experience, end_at, sent in ROWS:
        source_url = f"{LIST_URL}{raw_job_id}/"
        connection.execute(
            """
            INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)
            VALUES (?, 1, ?, '{}', ?)
            """,
            (raw_job_id, source_url, f"hash-{raw_job_id}"),
        )
        connection.execute(
            """
            INSERT INTO normalized_jobs
                (raw_job_id, company_name, title, source_url, experience_type, recruitment_end_at)
            VALUES (?, '예시회사', ?, ?, ?, ?)
            """,
            (raw_job_id, title, source_url, experience, end_at),
        )
        if sent:
            connection.execute(
                """
                INSERT INTO spring_deliveries (source_url, spring_job_id, status)
                VALUES (?, ?, 'sent')
                """,
                (source_url, raw_job_id),
            )
    connection.commit()
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
    return set(re.findall(r'class="job-title[^"]*">([^<]+)</span>', html))


def test_조회_조건에_경력_select가_있다(client: TestClient) -> None:
    html = client.get("/ui/review/filters").text

    assert 'name="experience"' in html
    assert ">경력</label>" in html
    assert '<option value="NEWCOMER">신입</option>' in html
    assert '<option value="EXPERIENCED">경력</option>' in html


def test_경력으로_좁히면_그_값을_가진_건만_나온다(client: TestClient) -> None:
    assert _titles(client, experience="NEWCOMER") == {
        "신입 마케터",
        "신입 개발자",
        "보내지 않은 신입",
    }
    assert _titles(client, experience="EXPERIENCED") == {"경력 마케터"}


def test_보냄과_마감일_없음에_경력을_겹쳐_건다(client: TestClient) -> None:
    assert _titles(client, view="sent", status="none", experience="NEWCOMER") == {"신입 마케터"}


def test_목록에_없는_경력_값은_조건을_걸지_않는다(client: TestClient) -> None:
    assert len(_titles(client, experience="'; DROP TABLE normalized_jobs; --")) == len(ROWS)


def test_조건_전체_고르기도_경력을_지킨다(conn: sqlite3.Connection) -> None:
    """보내기·다시 채우기·지우기가 같은 폼 값을 읽는다. 경력을 빠뜨리면 경력을 넘는다."""
    form = MultiDict(
        {"all_filtered": "1", "view": "sent", "status": "none", "experience": "NEWCOMER"}
    )

    assert form_raw_ids(conn, form) == [1]


def test_3년_이하는_자동_전송과_같은_연차로_거른다(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    """신입은 정규화가 0 으로, 연수를 말하지 않은 경력 공고는 빈 값으로 둔다. 둘 다 3년 이하다."""
    years = {1: "0", 2: "5", 3: "0", 4: "0", 5: None}
    for raw_job_id, value in years.items():
        conn.execute(
            "UPDATE normalized_jobs SET experience_min_years = ? WHERE raw_job_id = ?",
            (value, raw_job_id),
        )
    conn.execute(
        "INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)"
        " VALUES (6, 1, ?, '{}', 'hash-6')",
        (f"{LIST_URL}6/",),
    )
    conn.execute(
        """
        INSERT INTO normalized_jobs
            (raw_job_id, company_name, title, source_url, experience_type, experience_min_years)
        VALUES (6, '예시회사', '3년차 마케터', ?, 'EXPERIENCED', '3')
        """,
        (f"{LIST_URL}6/",),
    )
    conn.commit()

    assert _titles(client, experience="up_to_3") == {
        "신입 마케터",
        "신입 개발자",
        "보내지 않은 신입",
        "아직 분류 안 된 공고",
        "3년차 마케터",
    }
    html = client.get("/ui/review/filters").text
    assert '<option value="up_to_3">3년 이하</option>' in html
