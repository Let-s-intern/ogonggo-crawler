"""산업 분류 화면 (2026-09-14 결정).

직무 분류 화면(`tests/test_ui_taxonomy.py`)과 같은 자리다. 실사이트에 나가지 않는다.

| 확인 | 깨지면 |
|---|---|
| `정규화` 묶음에서 화면이 켜진다 | 산업 목록을 고칠 자리를 찾지 못한다 |
| 표가 비면 기본 산업 불러오기만 보이고, 누르면 11개가 들어온다 | 빈 표에서 무엇을 할지 모른다 |
| 더하고 고치고 끈다 | 산업 목록을 코드로만 바꾼다 |
| 겹친 이름은 사유와 함께 거절한다 | 저장이 조용히 실패한다 |
| 분류된 공고 수를 보이고 이름을 고치면 어긋난다고 알린다 | 이름과 공고 값이 몰래 갈린다 |
| 지우는 단추는 없다 | 분류된 공고가 목록 밖 값을 갖는다 |
"""

from __future__ import annotations

import html
import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import db, industries
from app.api.settings import get_connection
from app.main import app
from app.normalize.engine import insert_normalized


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (id, name, list_url, status)"
        " VALUES (1, '테스트', 'https://x', 'draft')"
    )
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, '테스트')")
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

    app.dependency_overrides[get_connection] = request_connection
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def add_classified_job(conn: sqlite3.Connection, seq: int, industry: str) -> None:
    """공고 한 건을 정규화까지 넣고 산업을 얹는다. 분류를 실제로 돌리지 않는다."""
    record = {"title": f"공고 {seq}", "body": "본문", "company_name": "테스트회사"}
    cursor = conn.execute(
        """
        INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash)
        VALUES (1, ?, ?, ?)
        """,
        (f"https://x/{seq}", json.dumps(record, ensure_ascii=False), f"hash-{seq}"),
    )
    normalized_id = insert_normalized(conn, int(cursor.lastrowid or 0), [])
    conn.execute("UPDATE normalized_jobs SET industry = ? WHERE id = ?", (industry, normalized_id))


def test_화면이_정규화_묶음에서_켜진다(client: TestClient) -> None:
    body = client.get("/industries").text

    assert '<a href="/rules" aria-current="page"' in body
    assert 'href="/industries" aria-current="page"' in body
    assert 'hx-get="/ui/industries"' in body


def test_표가_비어있으면_기본_산업_불러오기만_보인다(client: TestClient) -> None:
    body = client.get("/ui/industries").text

    assert "기본 산업 불러오기" in body
    assert "산업 분류가 아직 없다" in body
    assert "산업 추가" not in body


def test_기본_산업을_불러오면_열한_개가_들어오고_단추가_사라진다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    body = client.post("/ui/industries/seed").text

    assert "기본 산업 11개를 불러왔다" in body
    assert "기관·협회" in body
    assert "기본 산업 불러오기" not in body
    assert len(industries.list_all(conn)) == 11


def test_더하고_고치고_끈다(client: TestClient, conn: sqlite3.Connection) -> None:
    # 화면은 따옴표를 HTML 엔티티로 적는다
    added = html.unescape(client.post("/ui/industries", data={"name": "건설업"}).text)
    assert "'건설업' 를 더했다" in added
    (created,) = industries.list_all(conn)

    saved = html.unescape(
        client.put(
            f"/ui/industries/{created.id}", data={"name": "건설·토목업", "sort_order": "2"}
        ).text
    )
    assert "'건설·토목업' 를 저장했다" in saved

    toggled = html.unescape(client.post(f"/ui/industries/{created.id}/toggle").text)
    assert "'건설·토목업' 를 껐다" in toggled
    assert "꺼짐" in toggled
    assert industries.enabled_names(conn) == ()


def test_겹친_이름은_사유와_함께_거절한다(client: TestClient, conn: sqlite3.Connection) -> None:
    industries.create(conn, name="건설업")

    body = client.post("/ui/industries", data={"name": "건설업"}).text

    assert "duplicate_name" in body
    assert len(industries.list_all(conn)) == 1


def test_공고_수를_보이고_이름을_고치면_어긋난다고_알린다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    finance = industries.create(conn, name="금융·은행업")
    add_classified_job(conn, 1, "금융·은행업")
    add_classified_job(conn, 2, "금융·은행업")

    assert "2건" in client.get("/ui/industries").text

    body = client.put(f"/ui/industries/{finance.id}", data={"name": "금융업"}).text

    assert "이미 분류된 공고 2건은 새 이름과 어긋난다" in body


def test_지우는_단추는_없다(client: TestClient, conn: sqlite3.Connection) -> None:
    industries.create(conn, name="건설업")

    body = client.get("/ui/industries").text

    assert "hx-delete" not in body
    assert "삭제" not in body
