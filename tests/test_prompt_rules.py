"""분류 AI 규칙의 판 (2026-09-14 결정).

모델을 실제로 부르지 않는다. 규칙 한 벌이 판으로 쌓이고 읽히는지, 프롬프트에 실리는지, 분류 결과와
모델 호출에 판 번호가 남는지를 본다 (`app/classify/prompt_rules.py`).

| 확인 | 깨지면 |
|---|---|
| 저장한 판이 없으면 코드의 기본 규칙(판 0)이다 | 새 DB 에서 분류가 규칙 없이 돈다 |
| 기본 규칙이 칸 규칙·예시·고를 값으로 프롬프트에 실린다 | 옮기면서 옛 프롬프트 내용이 빠진다 |
| 고친 규칙이 실리고 기본 규칙 글은 빠진다 | 저장해도 모델에 가지 않는다 |
| 저장마다 새 판이고 같은 규칙은 판을 만들지 않는다 | 저장이 덮어쓰거나 같은 판이 쌓인다 |
| 되돌리기는 옛 판을 복사한 새 판이다 | 이력이 지워져 어느 규칙으로 분류했는지 모른다 |
| 판에 없는 칸은 기본 규칙으로 채운다 | 칸이 는 뒤 옛 판으로 분류하면 모델이 그 칸을 모른다 |
| 깨진 최근 판은 거절하고, 그 위에 새로 저장할 수 있다 | 화면의 규칙과 실제 규칙이 갈린다 |
| 반쪽 예시·너무 긴 규칙·모르는 칸을 거절한다 | 뜻 없는 예시가 프롬프트에 실린다 |
| 폼의 빈 예시 줄은 버리고 반쪽 줄은 남긴다 | 반쯤 적은 예시가 말없이 사라진다 |
| 분류 결과와 모델 호출에 판 번호가 남는다 | 어느 규칙으로 분류했는지 답할 수 없다 |
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest
from starlette.datastructures import FormData

from app import db
from app.classify import prompt_rules
from app.classify.batch import ClassifyProgress, classify_pending
from app.classify.classifier import build_prompt
from app.classify.prompt_rules import (
    DEFAULT_RULES,
    MAX_COMMON_CHARS,
    MAX_EXAMPLES,
    MAX_RULE_CHARS,
    Example,
    FieldRule,
    RuleSet,
    RuleSetError,
)
from tests.test_classify_run import GOOD, _seed, settings_with_key
from tests.test_selector_generator import FakeClient

TREE = [("IT·개발", ("서버·백엔드", "기타IT·개발"))]


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "rules.db")
    db.migrate_up(connection)
    try:
        yield connection
    finally:
        connection.close()


def _with_rule(rules: RuleSet, name: str, rule: FieldRule) -> RuleSet:
    return RuleSet(rules.common, {**rules.fields, name: rule})


def test_no_saved_version_means_the_code_defaults(conn: sqlite3.Connection) -> None:
    version = prompt_rules.current(conn)

    assert version.number == 0
    assert version.rules == DEFAULT_RULES
    assert prompt_rules.history(conn) == []


def test_default_rules_reach_the_prompt() -> None:
    prompt, _ = build_prompt("◆ 업무내용\n기획", "기획자 채용", None, TREE)

    assert "- responsibilities: 주요 업무·담당 업무" in prompt
    assert "  예: `채용 후 정규직 전환` → `인턴`" in prompt
    assert "  예: `고졸 이상` → `고졸`" in prompt
    assert "  고를 수 있는 값: " in prompt
    assert "# 공통 규칙" in prompt
    assert "- job_field: **가능하면 항상 채운다.**" in prompt
    # 자리표시자가 남으면 모델이 `{extract_rules}` 라는 글자를 규칙으로 읽는다
    assert "{extract_rules}" not in prompt
    assert "{common_rules}" not in prompt
    assert "{judge_rules}" not in prompt


def test_edited_rules_replace_the_defaults_in_the_prompt() -> None:
    edited = RuleSet(
        "- 시험용 공통 규칙",
        {
            **DEFAULT_RULES.fields,
            "region": FieldRule("근무지. 시험용 규칙", (Example("본사 근무", "서울"),)),
            "employment_type": FieldRule("시험용 고용형태 규칙"),
            "job_role": FieldRule("시험용 직무 규칙"),
        },
    )

    prompt, _ = build_prompt("◆ 업무내용\n기획", "기획자 채용", None, TREE, rules=edited)

    assert "- region: 근무지. 시험용 규칙" in prompt
    assert "  예: `본사 근무` → `서울`" in prompt
    assert "- 시험용 공통 규칙" in prompt
    assert "- employment_type: 시험용 고용형태 규칙" in prompt
    assert "- job_role: 시험용 직무 규칙" in prompt
    assert "채용 후 정규직 전환" not in prompt
    assert "슬로건·화면 UI 문구" not in prompt


def test_empty_common_rules_leave_no_section() -> None:
    prompt, _ = build_prompt("본문", "제목", None, (), rules=RuleSet("", DEFAULT_RULES.fields))

    assert "# 공통 규칙" not in prompt


def test_each_save_is_a_new_version_and_same_rules_are_refused(conn: sqlite3.Connection) -> None:
    # 저장한 판이 없을 때 기본 규칙을 그대로 저장하면 판 0 과 같다
    with pytest.raises(RuleSetError) as caught:
        prompt_rules.save(conn, DEFAULT_RULES)
    assert caught.value.reason == "unchanged"

    edited = _with_rule(DEFAULT_RULES, "region", FieldRule("근무지. 첫 판"))
    first = prompt_rules.save(conn, edited, "  근무지 규칙  ")
    assert (first.number, first.note) == (1, "근무지 규칙")
    assert prompt_rules.current(conn).rules == edited

    with pytest.raises(RuleSetError) as again:
        prompt_rules.save(conn, edited)
    assert again.value.reason == "unchanged"

    second = prompt_rules.save(conn, _with_rule(edited, "region", FieldRule("근무지. 둘째 판")))
    assert second.number == 2
    assert [entry.number for entry in prompt_rules.history(conn)] == [2, 1]


def test_restore_copies_an_old_version_into_a_new_one(conn: sqlite3.Connection) -> None:
    first = prompt_rules.save(conn, _with_rule(DEFAULT_RULES, "region", FieldRule("첫 판")))
    prompt_rules.save(conn, _with_rule(DEFAULT_RULES, "region", FieldRule("둘째 판")))

    restored = prompt_rules.restore(conn, first.number)

    assert restored.number == 3
    assert restored.rules == first.rules
    assert restored.note == "판 1 으로 되돌림"
    assert [entry.number for entry in prompt_rules.history(conn)] == [3, 2, 1]

    back = prompt_rules.restore(conn, 0)
    assert (back.number, back.rules) == (4, DEFAULT_RULES)

    with pytest.raises(RuleSetError) as caught:
        prompt_rules.restore(conn, 99)
    assert caught.value.reason == "not_found"


def test_a_field_missing_from_a_saved_version_gets_the_default(conn: sqlite3.Connection) -> None:
    stored = json.loads(
        prompt_rules.to_json(_with_rule(DEFAULT_RULES, "region", FieldRule("저장된 근무지 규칙")))
    )
    del stored["fields"]["benefits"]
    stored["fields"]["없어진칸"] = {"rule": "버린다", "examples": []}
    conn.execute(
        "INSERT INTO classify_rule_versions (rules_json) VALUES (?)",
        (json.dumps(stored, ensure_ascii=False),),
    )

    rules = prompt_rules.current(conn).rules

    assert rules.fields["benefits"] == DEFAULT_RULES.fields["benefits"]
    assert rules.fields["region"].rule == "저장된 근무지 규칙"
    assert "없어진칸" not in rules.fields


def test_a_broken_latest_version_is_refused_and_can_be_replaced(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO classify_rule_versions (rules_json) VALUES ('{깨짐')")

    with pytest.raises(RuleSetError) as caught:
        prompt_rules.current(conn)
    assert caught.value.reason == "broken_version"

    saved = prompt_rules.save(conn, _with_rule(DEFAULT_RULES, "region", FieldRule("고친 판")))
    assert saved.number == 2
    assert prompt_rules.current(conn).number == 2


@pytest.mark.parametrize(
    ("rules", "reason"),
    [
        (
            _with_rule(DEFAULT_RULES, "region", FieldRule("근무지", (Example("본사", ""),))),
            "half_example",
        ),
        (_with_rule(DEFAULT_RULES, "region", FieldRule("가" * (MAX_RULE_CHARS + 1))), "too_long"),
        (
            _with_rule(
                DEFAULT_RULES,
                "region",
                FieldRule(
                    "근무지", tuple(Example(f"원문{i}", "값") for i in range(MAX_EXAMPLES + 1))
                ),
            ),
            "too_many",
        ),
        (RuleSet("가" * (MAX_COMMON_CHARS + 1), DEFAULT_RULES.fields), "too_long"),
        (_with_rule(DEFAULT_RULES, "없는칸", FieldRule("규칙")), "unknown_rule_field"),
    ],
)
def test_rules_that_would_mislead_the_model_are_refused(
    conn: sqlite3.Connection, rules: RuleSet, reason: str
) -> None:
    with pytest.raises(RuleSetError) as caught:
        prompt_rules.save(conn, rules)

    assert caught.value.reason == reason
    assert prompt_rules.history(conn) == []


def test_form_drops_blank_example_rows_but_keeps_half_filled_ones() -> None:
    form = FormData(
        [
            ("common", "- 공통\r\n- 둘째"),
            ("rule__region", " 근무지 "),
            ("example_source__region", "본사 근무"),
            ("example_value__region", "서울"),
            ("example_source__region", ""),
            ("example_value__region", ""),
            ("example_source__region", "지사"),
            ("example_value__region", ""),
        ]
    )

    rules = prompt_rules.parse_form(form)

    assert rules.common == "- 공통\n- 둘째"
    assert rules.fields["region"] == FieldRule(
        "근무지", (Example("본사 근무", "서울"), Example("지사", ""))
    )
    assert rules.fields["benefits"] == FieldRule()
    with pytest.raises(RuleSetError) as caught:
        prompt_rules.validate(rules)
    assert caught.value.reason == "half_example"


@pytest.fixture
def seeded(conn: sqlite3.Connection) -> sqlite3.Connection:
    _seed(conn, count=1)
    return conn


async def test_classification_and_calls_record_the_saved_version(
    seeded: sqlite3.Connection,
) -> None:
    saved = prompt_rules.save(
        seeded, _with_rule(DEFAULT_RULES, "responsibilities", FieldRule("주요 업무. 시험용 규칙"))
    )
    client = FakeClient(GOOD)

    await classify_pending(seeded, ClassifyProgress(), client=client, settings=settings_with_key())

    assert "- responsibilities: 주요 업무. 시험용 규칙" in str(client.calls[0]["contents"])
    row = seeded.execute("SELECT rules_version FROM job_classifications").fetchone()
    assert row["rules_version"] == saved.number
    calls = seeded.execute("SELECT rules_version FROM llm_calls").fetchall()
    assert calls
    assert {call["rules_version"] for call in calls} == {saved.number}


async def test_without_a_saved_version_the_results_say_version_zero(
    seeded: sqlite3.Connection,
) -> None:
    await classify_pending(
        seeded, ClassifyProgress(), client=FakeClient(GOOD), settings=settings_with_key()
    )

    row = seeded.execute("SELECT rules_version FROM job_classifications").fetchone()
    assert row["rules_version"] == 0


async def test_a_broken_version_stops_the_batch_before_any_call(
    seeded: sqlite3.Connection,
) -> None:
    seeded.execute("INSERT INTO classify_rule_versions (rules_json) VALUES ('{깨짐')")
    progress = ClassifyProgress()
    client = FakeClient(GOOD)

    await classify_pending(seeded, progress, client=client, settings=settings_with_key())

    assert progress.failed == 1
    assert client.calls == []
    assert seeded.execute("SELECT COUNT(*) FROM job_classifications").fetchone()[0] == 0
