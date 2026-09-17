"""테스트 공통 설정.

Push 22 에서 운영 화면과 API 에 비밀번호 잠금이 붙었다 (`app/api/auth.py`). 기존 테스트는
30개 파일이 저마다 TestClient 를 만들어 잠긴 경로를 직접 부른다.

테스트에서 잠금을 끄는 스위치를 만들지 않는다. 그런 스위치는 운영에서 켜지는 순간 자물쇠가
통째로 없어지는 길이 되고, 잠금이 평소 동작을 깨는지도 확인하지 못한다. 대신 만들어지는 모든
TestClient 에 정상 서명된 쿠키를 하나 넣어 준다 — 미들웨어와 서명 검사를 전체 스위트가 매번
그대로 지나간다.

잠긴 쪽을 보는 테스트는 `client.cookies.clear()` 로 쿠키를 지우고 부른다
(`tests/test_admin_auth.py`).

## 기본 자동 분류는 테스트에서 걷어낸다

`0039` 가 "수집 직후 AI 분류" 를 켜진 채로 넣는다. 그대로 두면 크롤을 돌리는 테스트마다 분류
스레드가 떠 DB 를 붙잡고, 원문 다시 수집 테스트가 그 스레드를 기다리며 멈춘다. 그래서 테스트가
만드는 DB 는 마이그레이션 직후 그 행을 지운다. 분류가 이어지는지 보는 테스트는 워크플로우를 직접
만든다 (`tests/test_after_crawl_trigger.py`). 마이그레이션이 넣는지 자체는
`tests/test_migration_0039.py` 가 원래 함수로 본다.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import auth

DEFAULT_CLASSIFY = "수집 직후 AI 분류"


@pytest.fixture(autouse=True)
def admin_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """이 테스트에서 만들어지는 TestClient 에 유효한 세션 쿠키를 붙인다."""
    original_init = TestClient.__init__

    def init_with_session(self: TestClient, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.cookies.set(auth.COOKIE_NAME, auth.issue_token())

    monkeypatch.setattr(TestClient, "__init__", init_with_session)


@pytest.fixture(autouse=True)
def without_default_classify(monkeypatch: pytest.MonkeyPatch) -> None:
    """마이그레이션이 넣은 기본 자동 분류를 지운다. 위 설명 참고."""
    original = db.migrate_up

    def migrate_up(conn: Any, *args: Any, **kwargs: Any) -> list[str]:
        applied = original(conn, *args, **kwargs)
        if "0039" in applied:
            conn.execute("DELETE FROM side_workflows WHERE name = ?", (DEFAULT_CLASSIFY,))
            # 번호도 되돌린다. 1번 워크플로우를 가정하는 테스트가 있다
            conn.execute("DELETE FROM sqlite_sequence WHERE name = 'side_workflows'")
        return applied

    migrate_up.original = original  # type: ignore[attr-defined]
    monkeypatch.setattr(db, "migrate_up", migrate_up)
