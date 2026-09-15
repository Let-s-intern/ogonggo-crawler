"""설정의 하위 메뉴.

일곱이다 — AI 제공자 / 정규화 규칙 / 알림 / 파일 저장소 / 동시 실행 / 스냅샷 내보내기 /
데이터 가져오기. 정규화 규칙은 2026-09-15 에 설정으로 옮겼다. 하위 메뉴는 다른 묶음처럼
헤더 두 번째 줄이 그린다 (`app/api/ui.py` 의 `SETTINGS_NAV`).

여기서 보는 것은 셋이다. 일곱이 다 열리는가, 각 화면이 옮기기 전과 같은 조각을 부르는가,
그리고 어느 하위 화면에 있든 위 네비게이션이 `설정` 에 머무는가.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api.ui import SETTINGS_NAV
from app.main import app

# 하위 화면 하나가 부르는 자리. 옮기면서 잃어버리기 쉬운 문자열이다
CALLS: tuple[tuple[str, str], ...] = (
    ("/settings", 'hx-get="/ui/llm"'),
    ("/rules", 'hx-get="/ui/rules"'),
    ("/settings/notify", 'hx-get="/ui/notify"'),
    ("/settings/storage", 'hx-get="/ui/storage"'),
    ("/settings/runs", 'hx-get="/ui/settings"'),
    ("/settings/export", 'href="/ui/settings/export"'),
    ("/settings/import", 'hx-post="/ui/settings/import"'),
)


@pytest.fixture
def client() -> Iterator[TestClient]:
    yield TestClient(app, follow_redirects=False)


def test_하위_메뉴가_일곱이다() -> None:
    assert [label for _, label in SETTINGS_NAV] == [
        "AI 제공자",
        "정규화 규칙",
        "알림",
        "파일 저장소",
        "동시 실행",
        "스냅샷 내보내기",
        "데이터 가져오기",
    ]


@pytest.mark.parametrize(("path", "call"), CALLS)
def test_하위_화면이_옮기기_전과_같은_자리를_부른다(
    client: TestClient, path: str, call: str
) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert call in response.text


@pytest.mark.parametrize(("path", "_call"), CALLS)
def test_어느_하위_화면에서도_위_네비게이션은_설정이다(
    client: TestClient, path: str, _call: str
) -> None:
    """위 줄은 설정 묶음의 대표 주소가, 두 번째 줄은 그 화면 자신이 켜진다."""
    body = client.get(path).text

    assert '<a href="/settings" aria-current="page"' in body
    assert f'<a href="{path}" aria-current="page"' in body


def test_내보내기_화면이_파일에_키가_들어_있다고_알린다(client: TestClient) -> None:
    """숨기지 않고 알린다. 운영자가 모르고 남에게 주는 일만 막으면 된다 (2.5.V)."""
    body = client.get("/settings/export").text

    assert "이 파일에 API 키와 저장소 키가 들어 있습니다" in body
    assert "키도 같이 옮겨집니다" in body
    assert "키 재발급" in body
    assert not any(character in body for character in "✅❌⚠\U0001f4dd⭐")


@pytest.mark.parametrize(("path", "_call"), CALLS)
def test_하위_메뉴_일곱이_모든_화면에_있다(client: TestClient, path: str, _call: str) -> None:
    body = client.get(path).text

    for menu_path, label in SETTINGS_NAV:
        assert f'href="{menu_path}"' in body
        assert label in body
