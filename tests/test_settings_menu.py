"""설정의 하위 화면.

설정은 한 페이지의 왼쪽 목록이다 (2026-09-17, `app/api/ui.py` 의 `SETTINGS_SECTIONS`). 목록의
무리와 순서, 어느 화면에서든 위 메뉴가 `설정` 에 머무는지는 `test_ui_nav_groups.py` 가 본다.

여기서 보는 것은 둘이다. 각 화면이 옮기기 전과 같은 조각을 부르는가, 그리고 제목이 왼쪽
목록에서 켜진 이름과 같은가.
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


@pytest.mark.parametrize(("path", "call"), CALLS)
def test_하위_화면이_옮기기_전과_같은_자리를_부른다(
    client: TestClient, path: str, call: str
) -> None:
    response = client.get(path)

    assert response.status_code == 200
    assert call in response.text


@pytest.mark.parametrize(("path", "label"), SETTINGS_NAV)
def test_제목이_왼쪽_목록의_이름이다(client: TestClient, path: str, label: str) -> None:
    body = client.get(path).text

    assert f">{label}</h3>" in body


def test_내보내기_화면이_파일에_키가_들어_있다고_알린다(client: TestClient) -> None:
    """숨기지 않고 알린다. 운영자가 모르고 남에게 주는 일만 막으면 된다 (2.5.V)."""
    body = client.get("/settings/export").text

    assert "이 파일에 API 키와 저장소 키가 들어 있습니다" in body
    assert "키도 같이 옮겨집니다" in body
    assert "키 재발급" in body
    assert not any(character in body for character in "✅❌⚠\U0001f4dd⭐")
