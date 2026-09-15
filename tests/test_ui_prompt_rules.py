"""AI 규칙 화면 (2026-09-14 결정).

모델을 실제로 부르지 않는다. 시험 분류는 가짜 클라이언트가 답한다 (`app/api/ui_prompt_rules.py`).

| 확인 | 깨지면 |
|---|---|
| `정규화` 묶음에서 화면이 켜진다 | 규칙을 고칠 자리를 찾지 못한다 |
| 처음에는 판 0 과 기본 규칙 글·예시가 폼에 채워져 나온다 | 빈 폼에서 규칙을 처음부터 다시 쓴다 |
| 저장하면 새 판이 생긴다 | 저장이 조용히 사라진다 |
| 거절된 저장은 친 내용을 그대로 되그린다 | 고친 내용이 사유와 함께 날아간다 |
| 되돌리기는 새 판을 만든다 | 옛 판이 지워진다 |
| 시험 분류는 폼의 규칙으로 비교만 하고 저장하지 않는다 | 시험이 검수 값을 바꾼다 |
| 공고를 고르지 않은 시험은 사유를 댄다 | 빈 요청이 모델을 부르거나 500 이 난다 |
"""

from __future__ import annotations

import json
import pathlib
import re
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import ui_prompt_rules
from app.api.settings import get_connection
from app.classify import prompt_rules
from app.classify.prompt_rules import DEFAULT_RULES, Example, FieldRule, RuleSet
from app.classify.store import read_classification, save_classification
from app.main import app
from tests.test_classify_run import BODY, GOOD, settings_with_key
from tests.test_selector_generator import FakeClient

TITLE = "제휴 기획자"


@pytest.fixture
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "jobs.db"
    connection = db.connect(path)
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (id, name, list_url, status)"
        " VALUES (1, '테스트', 'https://x', 'promoted')"
    )
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, '테스트')")
    record = {
        "source_url": "https://x/1",
        "title": TITLE,
        "body": BODY,
        "company_name": "테스트회사",
    }
    connection.execute(
        """
        INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)
        VALUES (1, 1, 'https://x/1', ?, 'hash1')
        """,
        (json.dumps(record, ensure_ascii=False),),
    )
    # 지금 저장된 분류. 시험 결과와 칸마다 비교할 대상이다
    save_classification(
        connection,
        1,
        {"employment_type": "FULL_TIME", "responsibilities": "옛 업무 값"},
        model="옛모델",
        rules_version=0,
    )
    connection.close()
    return path


@pytest.fixture
def fake() -> FakeClient:
    return FakeClient(GOOD)


@pytest.fixture
def client(db_path: pathlib.Path, fake: FakeClient) -> Iterator[TestClient]:
    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(db_path)
        try:
            yield connection
        finally:
            connection.close()

    app.dependency_overrides[get_connection] = request_connection
    app.dependency_overrides[ui_prompt_rules.get_classify_client] = lambda: fake
    app.dependency_overrides[ui_prompt_rules.get_base_settings] = settings_with_key
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def conn(db_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(db_path)
    try:
        yield connection
    finally:
        connection.close()


def form_of(rules: RuleSet, **extra: str) -> dict[str, Any]:
    """화면 폼이 보내는 모양. 예시 칸은 같은 이름이 줄마다 되풀이된다."""
    data: dict[str, Any] = {"common": rules.common, **extra}
    for name, _, _ in prompt_rules.RULE_FIELDS:
        rule = rules.fields[name]
        data[f"rule__{name}"] = rule.rule
        data[f"example_source__{name}"] = [example.source for example in rule.examples]
        data[f"example_value__{name}"] = [example.value for example in rule.examples]
    return data


def _with_rule(name: str, rule: FieldRule) -> RuleSet:
    return RuleSet(DEFAULT_RULES.common, {**DEFAULT_RULES.fields, name: rule})


def _row(html: str, part: int, name: str) -> str:
    found = re.search(rf'id="prompt-rules-try-{part}-{name}">(.*?)</tr>', html, re.DOTALL)
    assert found is not None, f"{name} 줄이 시험 결과에 없다"
    return found.group(1)


def test_page_lights_up_in_the_classify_group(client: TestClient) -> None:
    body = client.get("/prompt-rules").text

    assert '<a href="/side" aria-current="page"' in body
    assert 'href="/prompt-rules" aria-current="page"' in body
    assert 'hx-get="/ui/prompt-rules"' in body


def test_first_view_shows_version_zero_filled_with_the_defaults(client: TestClient) -> None:
    html = client.get("/ui/prompt-rules").text

    assert "판 0 (코드의 기본 규칙" in html
    assert "주요 업무·담당 업무" in html
    assert 'value="채용 후 정규직 전환"' in html
    assert 'name="example_source__region"' in html
    # 시험에 고를 수 있는 공고는 이미 분류된 것이다
    assert f"#1 {TITLE}" in html


def test_saving_makes_a_new_version(client: TestClient, conn: sqlite3.Connection) -> None:
    edited = _with_rule(
        "region", FieldRule("근무지. 화면에서 고침", (Example("본사 근무", "서울"),))
    )

    html = client.put("/ui/prompt-rules", data=form_of(edited, note="근무지 규칙")).text

    assert "판 1 로 저장했다" in html
    assert "근무지. 화면에서 고침" in html
    saved = prompt_rules.current(conn)
    assert (saved.number, saved.note) == (1, "근무지 규칙")
    assert saved.rules.fields["region"] == edited.fields["region"]


def test_a_refused_save_redraws_what_was_typed(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    half = _with_rule("region", FieldRule("근무지. 반쪽 예시", (Example("본사 근무", ""),)))

    html = client.put("/ui/prompt-rules", data=form_of(half)).text

    assert "half_example" in html
    assert "근무지. 반쪽 예시" in html
    assert 'value="본사 근무"' in html
    assert prompt_rules.history(conn) == []


def test_restore_makes_a_new_version(client: TestClient, conn: sqlite3.Connection) -> None:
    first = prompt_rules.save(conn, _with_rule("region", FieldRule("첫 판")))
    prompt_rules.save(conn, _with_rule("region", FieldRule("둘째 판")))

    html = client.post(f"/ui/prompt-rules/{first.number}/restore").text

    assert "판 1 을 판 3 로 복사해 되돌렸다" in html
    assert prompt_rules.current(conn).rules == first.rules
    assert [entry.number for entry in prompt_rules.history(conn)] == [3, 2, 1]


def test_try_uses_the_form_rules_and_compares_without_saving(
    client: TestClient, conn: sqlite3.Connection, fake: FakeClient
) -> None:
    draft = _with_rule("responsibilities", FieldRule("주요 업무. 시험 중인 규칙"))

    html = client.post("/ui/prompt-rules/try", data=form_of(draft, raw_job_id="1")).text

    assert "- responsibilities: 주요 업무. 시험 중인 규칙" in str(fake.calls[0]["contents"])
    changed = _row(html, 1, "responsibilities")
    assert "옛 업무 값" in changed
    assert "제휴사 데이터 연동 구조 기획" in changed
    assert "달라짐" in changed
    assert "같음" in _row(html, 1, "employment_type")
    assert "결과는 저장하지 않았다" in html

    # 저장된 분류도 판도 그대로다. 호출은 판 없이 남는다
    assert read_classification(conn, 1)["responsibilities"] == "옛 업무 값"
    assert prompt_rules.history(conn) == []
    calls = conn.execute("SELECT rules_version FROM llm_calls").fetchall()
    assert calls
    assert {call["rules_version"] for call in calls} == {None}


def test_try_without_a_posting_says_so(client: TestClient, fake: FakeClient) -> None:
    html = client.post("/ui/prompt-rules/try", data=form_of(DEFAULT_RULES)).text

    assert "no_posting" in html
    assert fake.calls == []
