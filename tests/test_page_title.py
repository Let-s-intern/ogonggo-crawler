"""화면 제목.

탭 제목과 상단 제목에 같은 이름이 나온다. 조용히 사라지기 쉬운 문자열이라 여기서 잡는다.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.main import app

TITLE = "크롤링 자동화"

# 화면마다 제 이름을 붙이고 뒤에 서비스 이름을 단다
PAGES = ["/review", "/tests", "/workflows", "/rules", "/settings"]


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(app, follow_redirects=False)


@pytest.mark.parametrize("path", PAGES)
def test_tab_title_carries_the_service_name(client: TestClient, path: str) -> None:
    body = client.get(path).text

    assert f"— {TITLE}</title>" in body


@pytest.mark.parametrize("path", PAGES)
def test_heading_is_the_service_name(client: TestClient, path: str) -> None:
    body = client.get(path).text

    assert f">{TITLE}</h1>" in body
    assert "made by" not in body


def test_login_screen_carries_it_too(client: TestClient) -> None:
    """base.html 을 상속하지 않는 화면이라 따로 본다."""
    client.cookies.clear()
    body = client.get("/login").text

    assert f"<title>로그인 — {TITLE}</title>" in body
    assert f">{TITLE}</h1>" in body
    assert "made by" not in body
