"""위 네비게이션은 일의 흐름 순서로 묶였다 (2026-09-15 결정).

공고(수집한 결과를 본다) → 수집(사이트를 가져온다) → AI 분류(칸을 채운다) → 오공고 전송 →
설정(값을 넣어 둔다). 묶음 안의 실제 화면은 두 번째 줄(`group_nav`)에서 고른다
(`app/api/ui.py` 의 `NAV_GROUPS`). 대시보드와 오공고 전송은 하위 화면이 없어 묶지 않는다.
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import rules as rules_api
from app.api.ui import NAV, NAV_GROUPS
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
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_위_네비게이션은_흐름_순서다() -> None:
    assert [label for _, label in NAV] == [
        "대시보드",
        "공고",
        "수집",
        "AI 분류",
        "오공고 전송",
        "설정",
    ]


def test_묶음마다_들어_있는_화면() -> None:
    """완성 공고는 공고 목록으로 합쳤다 (2026-09-15)."""
    members = {name: [label for _, label in items] for _, name, items in NAV_GROUPS}

    assert members["공고"] == ["공고 목록", "회사 로고"]
    assert members["수집"] == ["워크플로우", "크롤러 등록", "테스트 실행"]
    assert members["AI 분류"] == ["분류 실행", "AI 규칙", "직무 분류", "산업 분류"]
    assert "정규화 규칙" in members["설정"]


@pytest.mark.parametrize(
    ("path_", "group_path"),
    [
        ("/review", "/review"),
        ("/companies", "/review"),
        ("/workflows", "/workflows"),
        ("/crawlers", "/workflows"),
        ("/tests", "/workflows"),
        ("/side", "/side"),
        ("/prompt-rules", "/side"),
        ("/taxonomy", "/side"),
        ("/industries", "/side"),
        ("/rules", "/settings"),
    ],
)
def test_묶인_화면은_위에서_자기_묶음이_켜진다(
    client: TestClient, path_: str, group_path: str
) -> None:
    """묶음의 대표 주소(`group_path`)가 위 네비게이션에서 `aria-current` 를 받는다."""
    body = client.get(path_).text

    assert f'<a href="{group_path}" aria-current="page"' in body


@pytest.mark.parametrize(
    ("path_", "own_label"),
    [
        ("/review", "공고 목록"),
        ("/companies", "회사 로고"),
        ("/workflows", "워크플로우"),
        ("/crawlers", "크롤러 등록"),
        ("/tests", "테스트 실행"),
        ("/side", "분류 실행"),
        ("/prompt-rules", "AI 규칙"),
        ("/taxonomy", "직무 분류"),
        ("/industries", "산업 분류"),
        ("/rules", "정규화 규칙"),
    ],
)
def test_묶인_화면은_두_번째_줄에서_자기_자리가_켜진다(
    client: TestClient, path_: str, own_label: str
) -> None:
    body = client.get(path_).text

    assert f'href="{path_}" aria-current="page"' in body
    assert own_label in body


def test_묶음_안의_다른_화면도_두_번째_줄에서_보인다(client: TestClient) -> None:
    """`/crawlers` 에 있어도 같은 묶음의 나머지가 눈에 보여야 늘어난 화면을 찾을 수 있다."""
    body = client.get("/crawlers").text

    for member_path, label in next(items for _, name, items in NAV_GROUPS if name == "수집"):
        assert f'href="{member_path}"' in body
        assert label in body


@pytest.mark.parametrize("path_", ["/", "/deliver"])
def test_묶이지_않은_화면에는_두_번째_줄이_없다(client: TestClient, path_: str) -> None:
    body = client.get(path_).text

    assert f'<a href="{path_}" aria-current="page"' in body
    assert 'aria-label="하위 메뉴"' not in body
