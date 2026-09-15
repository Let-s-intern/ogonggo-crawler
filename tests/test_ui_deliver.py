"""오공고(Spring) 전송 설정 화면.

저장소 검증은 `tests/test_deliver_settings.py`, 보내는 일은 `tests/test_deliver_spring.py` 가 본다.
여기서는 화면 조각이 그것들을 그대로 옮기는지만 본다.

| 확인 | 깨지면 |
|---|---|
| 무엇을 보내고 무엇을 보내지 않는지 적는다 | 운영자가 수정·삭제도 따라간다고 믿는다 |
| 키는 받지 않고 설정됐는지만 보인다 | 키가 화면·DB 로 샌다 |
| 저장한 값이 화면에서 다시 읽히고, 틀린 값은 사유와 함께 거절된다 | 저장이 조용히 실패한다 |
| 지금 보내기가 보내고 결과와 실패 사유를 보인다 | 눌러도 무엇이 갔는지 모른다 |
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db
from app.api.settings import get_connection
from app.config import Settings, get_settings
from app.deliver import spring
from app.main import app


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    try:
        yield connection
    finally:
        connection.close()


def client_with(tmp_path: pathlib.Path, key: str) -> TestClient:
    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(tmp_path / "jobs.db")
        try:
            yield connection
        finally:
            connection.close()

    app.dependency_overrides[get_connection] = request_connection
    app.dependency_overrides[get_settings] = lambda: Settings(ogonggo_internal_api_key=key)
    return TestClient(app)


@pytest.fixture
def client(tmp_path: pathlib.Path, conn: sqlite3.Connection) -> Iterator[TestClient]:
    try:
        yield client_with(tmp_path, "")
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def keyed(tmp_path: pathlib.Path, conn: sqlite3.Connection) -> Iterator[TestClient]:
    try:
        yield client_with(tmp_path, "test-internal-key")
    finally:
        app.dependency_overrides.clear()


def test_오공고_전송은_따로_열리는_화면이다(client: TestClient) -> None:
    """위 네비게이션에 바로 얹힌다. 하위 메뉴가 없다 (`app/api/ui.py` 의 `NAV`)."""
    body = client.get("/deliver").text

    assert '<a href="/deliver" aria-current="page"' in body
    assert 'hx-get="/ui/deliver"' in body
    assert 'aria-label="하위 메뉴"' not in body


def test_무엇을_보내고_보내지_않는지_적고_키는_받지_않는다(client: TestClient) -> None:
    body = client.get("/ui/deliver").text

    assert "아직 보내지 않은 공고를 오공고에 등록한다" in body
    assert "다시 보내지 않고" in body
    assert "OGONGGO_INTERNAL_API_KEY" in body
    assert "없음" in body
    assert 'name="auth_header"' not in body


def test_저장하면_화면에서_다시_읽힌다(client: TestClient) -> None:
    saved = client.put(
        "/ui/deliver",
        data={"url": "https://admin-api.example.com", "enabled": "1", "batch_size": "50"},
    )

    assert "저장했다. 분류 뒤 전송은 켜졌다" in saved.text
    reloaded = client.get("/ui/deliver").text
    assert 'value="https://admin-api.example.com"' in reloaded
    assert 'value="50"' in reloaded
    assert "checked" in reloaded


def test_잘못된_주소는_사유와_함께_거절된다(client: TestClient) -> None:
    response = client.put("/ui/deliver", data={"url": "ftp://x.example.com"})

    assert "처리하지 못했다" in response.text
    assert "http" in response.text


def test_키가_없으면_지금_보내기가_사유를_말한다(client: TestClient) -> None:
    client.put("/ui/deliver", data={"url": "https://admin-api.example.com"})

    body = client.post("/ui/deliver/send").text

    assert "OGONGGO_INTERNAL_API_KEY 가 비어 있다" in body


def test_지금_보내기가_보내고_실패_사유를_보인다(
    keyed: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyed.put("/ui/deliver", data={"url": "https://admin-api.example.com"})
    conn.execute("INSERT INTO crawlers (id, name, list_url) VALUES (1, '예시', 'https://x')")
    conn.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, '예시')")
    conn.execute(
        "INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)"
        " VALUES (1, 1, 'https://x/1', '{}', 'h1')"
    )
    conn.execute(
        """
        INSERT INTO normalized_jobs (raw_job_id, source_url, company_name, title, employment_type,
                                     experience_type, education_level, recruitment_type)
        VALUES (1, 'https://x/1', '예시', '공고', 'FULL_TIME', 'EXPERIENCED', 'ANY',
                'ALWAYS_OPEN')
        """
    )
    monkeypatch.setattr(
        spring,
        "transport",
        httpx.MockTransport(
            lambda request: httpx.Response(400, json={"message": "제목이 너무 길다"})
        ),
    )

    body = keyed.post("/ui/deliver/send").text

    assert "오공고에 0건 등록했다. 실패 1건" in body
    assert "400 제목이 너무 길다" in body
    assert "https://x/1" in body
