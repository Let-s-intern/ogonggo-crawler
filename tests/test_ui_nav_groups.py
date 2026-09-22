"""위 메뉴는 공고·사이트·부트캠프·비용·설정이다 (2026-09-17 결정 LC-3344, 부트캠프는 LC-3364).

첫 화면은 공고 목록이다. 설정 묶음은 왼쪽 목록으로(`SETTINGS_SECTIONS`) 자기 화면을 고른다.
사이트 추가·고치기는 사이트 목록의 창과 패널이 하고, 셀렉터를 손으로 다루는 두 화면(`SITE_PAGES`)은
탭 없이 링크로만 연다 (`app/api/ui.py`).
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import crawlers as crawlers_api
from app.api import rules as rules_api
from app.api.ui import NAV, SETTINGS_NAV, SETTINGS_SECTIONS, SITE_PAGES
from app.main import app


@pytest.fixture
def path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "jobs.db"


@pytest.fixture
def conn(path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(path)
    db.migrate_up(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def client(path: pathlib.Path, conn: sqlite3.Connection) -> Iterator[TestClient]:
    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(path)
        try:
            yield connection
        finally:
            connection.close()

    app.dependency_overrides[rules_api.get_connection] = request_connection
    app.dependency_overrides[crawlers_api.get_connection] = request_connection
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides.clear()


def test_위_메뉴() -> None:
    assert [label for _, label in NAV] == ["공고", "사이트", "부트캠프", "비용", "설정"]


def test_첫_화면은_공고_목록이다(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 307
    assert response.headers["location"] == "/review"


def test_설정_왼쪽_목록의_무리와_화면() -> None:
    sections = {name: [label for _, label in items] for name, items in SETTINGS_SECTIONS}

    assert sections == {
        "기본": ["AI", "오공고 전송", "실패 알림"],
        "수집·분류": [
            "수집 항목",
            "직무 분류",
            "산업 분류",
            "AI 분류 규칙",
            "정규화 규칙",
            "회사 로고",
        ],
        "시스템": ["자동 분류", "동시 실행", "파일 저장소", "스냅샷 내보내기", "데이터 가져오기"],
    }


@pytest.mark.parametrize("path_", ["/workflows", *(path for path, _ in SITE_PAGES)])
def test_사이트_화면은_위에서_사이트가_켜지고_탭은_없다(client: TestClient, path_: str) -> None:
    body = client.get(path_).text

    assert '<a href="/workflows" aria-current="page"' in body
    assert 'aria-label="하위 메뉴"' not in body
    assert 'aria-label="설정 메뉴"' not in body


@pytest.mark.parametrize(("path_", "label"), SETTINGS_NAV)
def test_설정_화면은_위에서_설정이_켜지고_왼쪽에서_자기_자리가_켜진다(
    client: TestClient, path_: str, label: str
) -> None:
    body = client.get(path_).text

    assert '<a href="/settings" aria-current="page"' in body
    assert 'aria-label="설정 메뉴"' in body
    assert f'<a href="{path_}" aria-current="page"' in body
    assert 'aria-label="하위 메뉴"' not in body
    for member, member_label in SETTINGS_NAV:
        assert f'href="{member}"' in body
        assert member_label in body


@pytest.mark.parametrize("path_", ["/review", "/cost"])
def test_묶이지_않은_화면에는_탭도_왼쪽_목록도_없다(client: TestClient, path_: str) -> None:
    body = client.get(path_).text

    assert f'<a href="{path_}" aria-current="page"' in body
    assert 'aria-label="하위 메뉴"' not in body
    assert 'aria-label="설정 메뉴"' not in body
