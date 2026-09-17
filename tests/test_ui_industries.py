"""산업 분류 화면 (2026-09-14 결정).

직무 분류 화면(`tests/test_ui_taxonomy.py`)과 같은 자리다. 실사이트에 나가지 않는다.

| 확인 | 깨지면 |
|---|---|
| `정규화` 묶음에서 화면이 켜진다 | 산업 목록을 고칠 자리를 찾지 못한다 |
| 표가 비면 기본 산업 불러오기만 보이고, 누르면 11개가 들어온다 | 빈 표에서 무엇을 할지 모른다 |
| 평소에는 칩, 수정을 누르면 줄마다 고치고 저장 한 번으로 저장한다 (2026-09-17) | 줄마다 저장한다 |
| 겹친 이름이면 아무것도 저장하지 않는다 | 절반만 저장된다 |
| 이름을 바꾸면 이미 분류된 공고의 값도 바뀐다 | 이름과 공고 값이 몰래 갈린다 |
| 메모 칸과 지우는 단추는 없다 | 분류된 공고가 목록 밖 값을 갖는다 |
"""

from __future__ import annotations

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


def test_화면이_설정에서_켜진다(client: TestClient) -> None:
    body = client.get("/industries").text

    assert '<a href="/settings" aria-current="page"' in body
    assert 'href="/industries" aria-current="page"' in body
    assert 'hx-get="/ui/industries"' in body


def test_기본_산업을_불러오면_열한_개가_들어오고_단추가_사라진다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    assert "기본 산업 불러오기" in client.get("/ui/industries").text

    body = client.post("/ui/industries/seed").text

    assert "기본 산업 11개를 불러왔다" in body
    assert "기관·협회" in body
    assert "기본 산업 불러오기" not in body
    assert len(industries.list_all(conn)) == 11


def test_평소에는_칩이고_수정을_누르면_줄마다_고쳐_한_번에_저장한다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    it = industries.create(conn, name="IT·정보통신업", sort_order=0)
    finance = industries.create(conn, name="금융·은행업", sort_order=1)

    view = client.get("/ui/industries").text
    edit = client.get("/ui/industries?edit=true").text
    body = client.put(
        "/ui/industries",
        data={
            "industry_id": [str(finance.id), str(it.id), ""],
            "industry_name": ["금융업", "IT·정보통신업", "게임업"],
            "industry_on": ["0", "1", "1"],
        },
    ).text

    assert view.count("industry-chip") == 2 and 'name="industry_name"' not in view
    assert edit.count('name="industry_name"') == 3  # 두 줄과 새 줄 틀
    assert "메모" not in view + edit and "삭제" not in view + edit
    assert "저장했습니다" in body
    assert [(item.name, item.enabled) for item in industries.list_all(conn)] == [
        ("금융업", False),
        ("IT·정보통신업", True),
        ("게임업", True),
    ]


def test_이름을_바꾸면_이미_분류된_공고의_값도_바뀐다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    it = industries.create(conn, name="IT·정보통신업")
    add_classified_job(conn, 1, "IT·정보통신업")

    body = client.put(
        "/ui/industries",
        data={"industry_id": [str(it.id)], "industry_name": ["IT업"], "industry_on": ["1"]},
    ).text

    assert "IT·정보통신업 → IT업" in body
    assert conn.execute("SELECT industry FROM normalized_jobs").fetchone()[0] == "IT업"


def test_겹친_이름이면_아무것도_저장하지_않는다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    it = industries.create(conn, name="IT·정보통신업")
    finance = industries.create(conn, name="금융·은행업")

    body = client.put(
        "/ui/industries",
        data={
            "industry_id": [str(it.id), str(finance.id)],
            "industry_name": ["같은이름", "같은이름"],
            "industry_on": ["1", "1"],
        },
    ).text

    assert "같은 이름이 두 번 있다" in body
    assert [item.name for item in industries.list_all(conn)] == ["IT·정보통신업", "금융·은행업"]
