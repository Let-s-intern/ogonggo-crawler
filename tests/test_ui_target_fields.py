"""공고 목록 직군 칸의 수집·전송 대상 스위치 (`app/api/ui_target_fields.py`, 2026-10-19).

| 확인 | 깨지면 |
|---|---|
| 직군 칸에 직군마다 이름과 스위치가 있다 | 공고 목록에서 대상 직군을 바꿀 수 없다 |
| 스위치를 누르면 저장하고 바뀐 줄을 돌려준다 | 화면이 저장된 값과 어긋난다 |
| 마지막 직군은 끄지 못하고 사유를 보인다 | 아무것도 모으지 않는 설정이 조용히 저장된다 |
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import db, taxonomy
from app.api import crawlers
from app.api.ui_taxonomy import SEED_PATH
from app.crawler import target_fields
from app.main import app


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    taxonomy.load_seed(connection, SEED_PATH)
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

    app.dependency_overrides[crawlers.get_connection] = request_connection
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_직군_칸에_직군마다_이름과_스위치가_있다(client: TestClient) -> None:
    html = client.get("/ui/review/filters").text

    assert 'data-job-field="게임"' in html
    assert html.count('role="switch"') == 19
    assert html.count('aria-checked="true"') == len(target_fields.DEFAULT_FIELDS)
    assert "수집 5" in html


def test_스위치를_누르면_저장하고_바뀐_줄을_돌려준다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    html = client.post("/ui/target-fields", data={"field": "게임", "on": "true"}).text

    assert "게임" in target_fields.read_fields(conn)
    assert 'aria-label="게임 수집 켜짐"' in html
    assert "켜 둔 직군 6개" in html

    client.post("/ui/target-fields", data={"field": "영업", "on": "false"})
    assert "영업" not in target_fields.read_fields(conn)


def test_마지막_직군은_끄지_못하고_사유를_보인다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    target_fields.write_fields(conn, ["교육"])

    html = client.post("/ui/target-fields", data={"field": "교육", "on": "false"}).text

    assert "하나는 켜 둬야 한다" in html
    assert target_fields.read_fields(conn) == ("교육",)
