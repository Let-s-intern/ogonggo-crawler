"""정규화 규칙 화면이 고르게 두는 필드 목록 (7.3.V).

실사이트에 나가지 않는다. 저장된 규칙을 넣고 화면 경로로만 연다.

화면의 목록과 `normalized_jobs` 의 칸이 어긋나면 운영자는 저장할 수 없는 규칙을 만들게 된다.
고를 수 있으니 고르고, 설정을 적고, 저장을 누른 뒤에야 API 가 거절한다. 그 실패는 규칙을 다
쓴 다음에 나므로 되돌릴 것이 많다.

0016 이 `department`·`job_category`·`headcount` 를 지웠다. 지운 칸의 규칙은 같은
마이그레이션이 먼저 지우고(`migrations/0016_drop_department_category_headcount.sql`),
가져오기도 들이지 않는다(`app/api/import_data.py`). 화면이 마지막 자리다.

| 확인 | 깨지면 |
|---|---|
| 새 규칙의 필드 목록이 `NORMALIZED_FIELDS` 와 같다 | 저장할 수 없는 규칙을 만들게 된다 |
| 0016 이 지운 세 칸이 목록에 없다 | 지운 칸에 규칙이 다시 쌓인다 |
| 0017 이 더한 직무를 고를 수 있다 | 새 칸만 규칙을 걸 길이 없다 |
| 기존 규칙의 필드 목록도 같다 | 규칙 하나를 고치다 지운 칸으로 옮기게 된다 |
| 목록에 없는 칸은 API 가 거절한다 | 화면만 막고 경로는 열려 있다 |
"""

from __future__ import annotations

import pathlib
import re
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import rules as rules_api
from app.main import app
from app.normalize.rules import NORMALIZED_FIELDS

# 0016 이 `normalized_jobs` 에서 지운 칸
DROPPED_FIELDS = ("department", "job_category", "headcount")

NEW_RULE_SELECT = re.compile(r'<select id="new-field" name="field_name">(.*?)</select>', re.DOTALL)
EXISTING_SELECT = re.compile(
    r'<select name="field_name" form="rule-form-\d+">(.*?)</select>', re.DOTALL
)
OPTION = re.compile(r'<option value="([^"]+)"')


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        """
        INSERT INTO normalization_rules (field_name, rule_type, rule_config_json, priority, enabled)
        VALUES ('title', 'trim', '{"collapse_whitespace": true}', 0, 1)
        """
    )
    connection.commit()
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

    app.dependency_overrides[rules_api.get_connection] = request_connection
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _options(pattern: re.Pattern[str], html: str) -> list[str]:
    found = pattern.search(html)
    assert found is not None, "필드를 고르는 칸이 화면에 없다"
    return OPTION.findall(found.group(1))


def test_새_규칙의_필드_목록이_정규화_칸과_같다(client: TestClient) -> None:
    """순서까지 본다. 목록의 순서가 곧 운영자가 칸을 찾는 순서다."""
    html = client.get("/ui/rules").text

    assert _options(NEW_RULE_SELECT, html) == list(NORMALIZED_FIELDS)


def test_지운_세_칸은_고를_수_없고_직무는_고를_수_있다(client: TestClient) -> None:
    options = _options(NEW_RULE_SELECT, client.get("/ui/rules").text)

    for field in DROPPED_FIELDS:
        assert field not in options
    # 직무 분류 소분류다. 0031 전의 자유 글자 직무(`position_name`)는 규칙을 걸 칸이 아니다
    assert "job_role" in options
    assert "position_name" not in options


def test_기존_규칙의_필드_목록도_같다(client: TestClient) -> None:
    """줄마다 있는 목록이 새 규칙 쪽과 갈리면 한쪽에서만 지운 칸으로 옮길 수 있다."""
    html = client.get("/ui/rules").text

    assert _options(EXISTING_SELECT, html) == list(NORMALIZED_FIELDS)


@pytest.mark.parametrize("field", DROPPED_FIELDS)
def test_지운_칸의_규칙은_저장이_거절된다(client: TestClient, field: str) -> None:
    """화면에서 빠졌더라도 경로가 열려 있으면 지운 칸에 규칙이 다시 쌓인다."""
    response = client.post(
        "/api/rules", json={"field_name": field, "rule_type": "trim", "rule_config": {}}
    )

    assert response.status_code == 422
