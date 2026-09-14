"""위 네비게이션이 묶음으로 줄어든 것.

화면이 늘 때마다 위 줄이 계속 길어지지 않게, `NAV` 는 묶음 이름만 놓고 각 묶음의 실제
화면은 두 번째 줄(`group_nav`)에서 고른다 (`app/api/ui.py` 의 `NAV_GROUPS`).

묶음은 파이프라인 단계로 가른다 — 수집(사이트를 가져온다), 정규화(가져온 것을 다듬는다),
데이터 확인(다듬은 결과를 본다), 운영 설정(값을 넣어 둔다). 부가 워크플로우는 사이트를
가져오지 않고 이미 가져온 것을 가공하므로 "수집" 이 아니라 "정규화" 에 있다
(2026-08-29 결정).
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


def test_위_네비게이션은_다섯이다() -> None:
    """대시보드는 하위 화면이 없어 `/settings` 처럼 묶이지 않고 맨 앞에 직접 얹힌다."""
    assert [label for _, label in NAV] == ["대시보드", "수집", "정규화", "데이터 확인", "운영 설정"]


def test_묶음_안의_화면을_전부_합치면_열이다() -> None:
    """줄어든 것은 위 줄뿐이다. 화면 자체는 열 + 운영 설정이다(직무 분류와 AI 규칙이 더해졌다)."""
    grouped = sum(len(members) for _, _, members in NAV_GROUPS)
    assert grouped == 10


def test_부가_워크플로우는_수집이_아니라_정규화_묶음에_있다() -> None:
    """사이트를 가져오지 않고 이미 가져온 것을 가공한다 (2026-08-29 결정)."""
    collect_group = next(members for _, name, members in NAV_GROUPS if name == "수집")
    normalize_group = next(members for _, name, members in NAV_GROUPS if name == "정규화")

    assert ("/side", "부가 워크플로우") not in collect_group
    assert ("/side", "부가 워크플로우") in normalize_group


@pytest.mark.parametrize(
    ("path_", "group_path"),
    [
        ("/crawlers", "/crawlers"),
        ("/tests", "/crawlers"),
        ("/workflows", "/crawlers"),
        ("/rules", "/rules"),
        ("/side", "/rules"),
        ("/taxonomy", "/rules"),
        ("/prompt-rules", "/rules"),
        ("/review", "/review"),
        ("/companies", "/review"),
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
        ("/crawlers", "크롤러 등록"),
        ("/tests", "테스트 실행"),
        ("/workflows", "워크플로우"),
        ("/rules", "정규화 규칙"),
        ("/side", "부가 워크플로우"),
        ("/taxonomy", "직무 분류"),
        ("/prompt-rules", "AI 규칙"),
        ("/review", "데이터 확인"),
        ("/companies", "회사 로고"),
    ],
)
def test_묶인_화면은_두_번째_줄에서_자기_자리가_켜진다(
    client: TestClient, path_: str, own_label: str
) -> None:
    body = client.get(path_).text

    assert f'href="{path_}" aria-current="page"' in body
    assert own_label in body


def test_묶음_안의_다른_화면도_두_번째_줄에서_보인다(client: TestClient) -> None:
    """`/crawlers` 에 있어도 같은 묶음의 나머지가 눈에 보여야, 늘어난 화면을 찾을 수 있다."""
    body = client.get("/crawlers").text

    for member_path, label in next(members for _, name, members in NAV_GROUPS if name == "수집"):
        assert f'href="{member_path}"' in body
        assert label in body


def test_운영_설정은_묶이지_않고_그대로다(client: TestClient) -> None:
    body = client.get("/settings").text

    assert '<a href="/settings" aria-current="page"' in body
    # 운영 설정은 자기 하위 메뉴(SETTINGS_NAV)만 그린다. 두 번째 묶음 줄과 섞이지 않는다
    assert "정규화 규칙" not in body


def test_대시보드는_묶이지_않고_그대로다(client: TestClient) -> None:
    """대시보드도 `/settings` 와 같은 이유로 묶이지 않는다 — 하위 화면이 없다."""
    body = client.get("/").text

    assert '<a href="/" aria-current="page"' in body
    # 두 번째 묶음 줄(`group_nav`) 자체가 없다. 화면 초기 응답에는 조각이 아직 안 들어와
    # 자주 쓰는 화면 목록의 문구와 섞일 걱정 없이 마크업으로 바로 확인할 수 있다
    assert '<nav class="mt-2">' not in body
