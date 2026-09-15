"""정규화 규칙 화면이 고르게 두는 칸 (2026-09-15 에 수집 칸 다섯으로 좁혔다).

실사이트에 나가지 않는다. 저장된 규칙을 넣고 화면 경로로만 연다.

AI 분류가 덮어쓰는 칸에 건 규칙은 아무 일도 하지 않는다. 화면이 그 칸을 고르게 두면 운영자는
먹지 않는 규칙을 쓰고, 왜 값이 안 바뀌는지 찾아 헤맨다.

| 확인 | 깨지면 |
|---|---|
| 고를 수 있는 칸이 `RULE_FIELDS` 와 같다 | 먹지 않는 칸에 규칙을 건다 |
| 규칙 칸은 정규화 칸에서 분류 칸과 대표 이미지를 뺀 것이다 | 분류 칸이 늘면 어긋난다 |
| 칸 이름이 한글로 보인다 | 영어 칼럼 이름을 외워야 한다 |
| 분류 칸에 걸린 옛 규칙은 `효과 없음` 이고 고칠 칸이 없다 | 먹지 않는 규칙을 계속 고친다 |
| 미리보기도 같은 칸만 고른다 | 미리보기에서만 다른 칸이 보인다 |
| 0016 이 지운 칸의 규칙은 API 가 거절한다 | 지운 칸에 규칙이 다시 쌓인다 |
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
from app.classify.schema import STORED_CLASSIFY_FIELDS
from app.main import app
from app.normalize.rules import NORMALIZED_FIELDS, RULE_FIELDS

# 0016 이 `normalized_jobs` 에서 지운 칸
DROPPED_FIELDS = ("department", "job_category", "headcount")

NEW_RULE_SELECT = re.compile(r'<select id="new-field" name="field_name">(.*?)</select>', re.DOTALL)
EXISTING_SELECT = re.compile(
    r'<select name="field_name" form="rule-form-(\d+)">(.*?)</select>', re.DOTALL
)
PREVIEW_SELECT = re.compile(r'<select name="field_name">(.*?)</select>', re.DOTALL)
OPTION = re.compile(r'<option value="([^"]+)"')


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        """
        INSERT INTO normalization_rules (id, field_name, rule_type, rule_config_json, priority,
                                         enabled)
        VALUES (1, 'title', 'trim', '{"collapse_whitespace": true}', 0, 1),
               (2, 'qualifications', 'trim', '{"collapse_whitespace": true}', 0, 0)
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
    assert found is not None, "칸을 고르는 칸이 화면에 없다"
    return OPTION.findall(found.group(found.lastindex or 0))


def test_규칙_칸은_정규화_칸에서_분류가_덮는_칸과_대표_이미지를_뺀_것이다() -> None:
    expected = [
        name
        for name in NORMALIZED_FIELDS
        if name not in STORED_CLASSIFY_FIELDS and name != "cover_image_url"
    ]
    assert sorted(RULE_FIELDS) == sorted(expected)


def test_새_규칙의_칸_목록이_규칙_칸과_같다(client: TestClient) -> None:
    html = client.get("/ui/rules").text

    assert _options(NEW_RULE_SELECT, html) == list(RULE_FIELDS)
    assert ">회사명</option>" in html and ">모집 마감</option>" in html


def test_분류가_채우는_칸과_지운_칸은_고를_수_없다(client: TestClient) -> None:
    options = _options(NEW_RULE_SELECT, client.get("/ui/rules").text)

    for field in (*DROPPED_FIELDS, "qualifications", "employment_type", "job_role", "industry"):
        assert field not in options


def test_기존_규칙의_칸_목록도_같다(client: TestClient) -> None:
    html = client.get("/ui/rules").text

    found = EXISTING_SELECT.findall(html)
    assert [rule_id for rule_id, _ in found] == ["1"]
    assert OPTION.findall(found[0][1]) == list(RULE_FIELDS)


def test_분류_칸에_걸린_옛_규칙은_효과_없음이고_고칠_칸이_없다(client: TestClient) -> None:
    html = client.get("/ui/rules").text

    assert "효과 없음" in html
    assert 'id="rule-form-2"' not in html
    # 지우기는 할 수 있다
    assert 'hx-delete="/ui/rules/2"' in html


def test_미리보기도_규칙_칸만_고른다(client: TestClient) -> None:
    html = client.get("/ui/rules/preview").text

    assert _options(PREVIEW_SELECT, html) == list(RULE_FIELDS)


@pytest.mark.parametrize("field", DROPPED_FIELDS)
def test_지운_칸의_규칙은_저장이_거절된다(client: TestClient, field: str) -> None:
    """화면에서 빠졌더라도 경로가 열려 있으면 지운 칸에 규칙이 다시 쌓인다."""
    response = client.post(
        "/api/rules", json={"field_name": field, "rule_type": "trim", "rule_config": {}}
    )

    assert response.status_code == 422
