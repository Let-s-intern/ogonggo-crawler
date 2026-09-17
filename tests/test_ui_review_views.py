"""공고 목록의 보기(오늘 들어옴·확인 필요·보냄·전체)와 위 숫자 카드 (2026-09-17, LC-3344).

실사이트에 나가지 않는다. 저장된 행을 넣고 화면 경로로만 조회한다.

| 확인 | 깨지면 |
|---|---|
| 오늘 들어옴은 표시 시간대 오늘 0시 뒤에 수집한 것이다 | 어제 것이 섞이거나 오늘 것이 빠진다 |
| 확인 필요는 보내지 않았고 마감 전인 것 중 넷 중 하나에 걸린 것이다 | 손볼 공고를 못 찾는다 |
| 행마다 어디에 걸렸는지 적는다 | 왜 확인이 필요한지 모른다 |
| 켜진 사이트의 마지막 수집이 실패하면 알린다 | 공고가 안 들어오는 이유를 모른다 |
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
LONG_BODY = "본문 " * 150
READY = {
    "employment_type": "FULL_TIME",
    "experience_type": "NEW",
    "education_level": "BACHELOR",
    "recruitment_type": "ALWAYS",
}


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES ('site', ?, 'promoted')", (LIST_URL,)
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '잘 되는 곳')")
    connection.execute(
        "INSERT INTO workflows (crawler_id, name, status) VALUES (1, '멈춘 곳', 'paused')"
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '고장 난 곳')")
    try:
        yield connection
    finally:
        connection.close()


def add_job(
    conn: sqlite3.Connection,
    raw_job_id: int,
    title: str,
    *,
    crawled_at: str = "datetime('now')",
    classified: bool = True,
    ready: bool = True,
    body: str = LONG_BODY,
    end_at: str | None = "2099-12-31 23:59:59",
    delivery: str | None = None,
) -> None:
    source_url = f"{LIST_URL}{raw_job_id}/"
    conn.execute(
        f"""
        INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash, crawled_at)
        VALUES (?, 1, ?, '{{}}', ?, {crawled_at})
        """,
        (raw_job_id, source_url, f"hash-{raw_job_id}"),
    )
    values = READY if ready else {}
    conn.execute(
        """
        INSERT INTO normalized_jobs (raw_job_id, company_name, title, body, recruitment_end_at,
                                     source_url, employment_type, experience_type,
                                     education_level, recruitment_type)
        VALUES (?, '회사', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            raw_job_id,
            title,
            body,
            end_at,
            source_url,
            values.get("employment_type"),
            values.get("experience_type"),
            values.get("education_level"),
            values.get("recruitment_type"),
        ),
    )
    if classified:
        conn.execute(
            "INSERT INTO job_classifications (raw_job_id, model) VALUES (?, 'test')", (raw_job_id,)
        )
    if delivery == "sent":
        conn.execute(
            "INSERT INTO spring_deliveries (source_url, status, sent_at)"
            " VALUES (?, 'sent', datetime('now'))",
            (source_url,),
        )
    elif delivery == "sent_long_ago":
        conn.execute(
            "INSERT INTO spring_deliveries (source_url, status, sent_at)"
            " VALUES (?, 'sent', '2000-01-01 00:00:00')",
            (source_url,),
        )
    elif delivery == "failed":
        conn.execute(
            "INSERT INTO spring_deliveries (source_url, status, attempts, last_error)"
            " VALUES (?, 'failed', 1, '400 마감일이 비었다')",
            (source_url,),
        )


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    add_job(conn, 1, "정상 대기")
    add_job(conn, 2, "보낸 공고", delivery="sent")
    add_job(
        conn, 3, "예전에 보낸 공고", crawled_at="'2000-01-01 00:00:00'", delivery="sent_long_ago"
    )
    add_job(conn, 4, "거절된 공고", delivery="failed")
    add_job(conn, 5, "분류 전 공고", classified=False, ready=False)
    add_job(conn, 6, "칸 빈 공고", ready=False)
    add_job(conn, 7, "본문 짧은 공고", body="요약 한 줄")
    add_job(conn, 8, "마감 지난 칸 빈 공고", ready=False, end_at="2000-01-01 00:00:00")
    add_job(conn, 9, "어제 들어온 칸 빈 공고", crawled_at="datetime('now', '-3 days')", ready=False)
    conn.commit()
    return conn


@pytest.fixture
def client(tmp_path: pathlib.Path, seeded: sqlite3.Connection) -> Iterator[TestClient]:
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


def titles(client: TestClient, **params: str) -> set[str]:
    return set(TITLE_CELL.findall(client.get("/ui/review", params=params).text))


def row_of(html: str, title: str) -> str:
    start = html.index(f">{title}</span>")
    return html[html.rindex("<tr>", 0, start) : html.index("</tr>", start)]


def test_보기가_없으면_전체다(client: TestClient) -> None:
    assert len(titles(client)) == 9
    assert titles(client, view="all") == titles(client)


def test_오늘_들어옴은_오늘_수집한_것만이다(client: TestClient) -> None:
    today = titles(client, view="today")

    assert "예전에 보낸 공고" not in today
    assert "어제 들어온 칸 빈 공고" not in today
    assert len(today) == 7


def test_확인_필요는_넷_중_하나에_걸린_보내지_않은_마감_전_공고다(client: TestClient) -> None:
    assert titles(client, view="check") == {
        "거절된 공고",
        "분류 전 공고",
        "칸 빈 공고",
        "본문 짧은 공고",
        "어제 들어온 칸 빈 공고",
    }


def test_보냄은_보낸_공고_전부다(client: TestClient) -> None:
    assert titles(client, view="sent") == {"보낸 공고", "예전에 보낸 공고"}


def test_모르는_보기는_조건을_걸지_않는다(client: TestClient) -> None:
    assert titles(client, view="nope") == titles(client)


def test_행마다_어디에_걸렸는지_적는다(client: TestClient) -> None:
    html = client.get("/ui/review").text

    assert "오공고 보냄" in row_of(html, "보낸 공고")
    assert "전송 실패" in row_of(html, "거절된 공고")
    assert "AI 분류 안 됨" in row_of(html, "분류 전 공고")
    assert "필수 칸 빔" not in row_of(html, "분류 전 공고")
    assert "필수 칸 빔" in row_of(html, "칸 빈 공고")
    assert "본문 짧음" in row_of(html, "본문 짧은 공고")
    assert "보내기 전" in row_of(html, "정상 대기")


def test_칩에_보기마다_건수가_있다(client: TestClient) -> None:
    html = client.get("/ui/review/filters").text

    for label, number in (("오늘 들어옴", 7), ("확인 필요", 5), ("보냄", 2), ("전체", 9)):
        assert re.search(rf"{label} <span[^>]*>{number}</span>", html), label


def test_숫자_카드는_오늘_들어옴_확인_필요_오늘_보냄이다(client: TestClient) -> None:
    html = client.get("/ui/review/summary").text

    def number(label: str) -> int:
        found = re.search(rf"{label}</span>\s*<span[^>]*>(\d+)<", html)
        assert found is not None, label
        return int(found.group(1))

    assert number("오늘 들어온 공고") == 7
    assert number("확인이 필요한 공고") == 5
    assert number("오늘 오공고로 보냄") == 1
    for view in ("today", "check", "sent"):
        assert f'data-view="{view}"' in html


def test_켜진_사이트의_마지막_수집이_실패하면_알린다(
    client: TestClient, seeded: sqlite3.Connection
) -> None:
    assert "수집이 실패했어요" not in client.get("/ui/review/summary").text

    seeded.executemany(
        "INSERT INTO crawl_runs (workflow_id, status) VALUES (?, ?)",
        [(1, "failed"), (1, "success"), (2, "failed"), (3, "success"), (3, "failed")],
    )
    seeded.commit()
    html = client.get("/ui/review/summary").text

    assert "수집이 실패했어요" in html
    assert "고장 난 곳" in html
    assert "잘 되는 곳" not in html
    assert "멈춘 곳" not in html
    assert 'href="/workflows"' in html
