"""정규화 단계의 회사명 두 칸 테스트.

칸이 갈린 뒤로 합치는 일이 없다. 확인하는 것은 일곱이다.

- `parent_company_name` 는 `crawlers.default_company` 그대로다. 2026-08-29 부터 등록·수정 화면이
  이 칸을 필수로 받으므로, 비어 있으면 크롤러 이름을 대신 쓰던 옛 동작(2026-08-26 결정)은
  더 이상 쓰지 않는다 — 비어 있으면 그대로 NULL 이다
- `company_name` 는 공고에서 뽑은 값 그대로다. 모회사가 그 자리를 메우지 않는다
- 사이트가 회사명을 주지 않으면 `company_name` 는 NULL 이고 `parent_company_name` 만 남는다
- 둘 다 없으면 둘 다 NULL 이다. 빈 문자열로 채우지 않는다
- 계열사 두 건이 섞인 목록에서 두 건이 서로 다른 자회사를, 같은 모회사를 받는다
- 저장소가 싣고 나가는 `company_name` 규칙 넷은 그대로 자회사에 걸린다
- `parent_company_name` 는 규칙을 타지 않고, 그 칸에 규칙을 만들 수도 없다

픽스처로 돈다. 실사이트에 나가지 않는다.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3

import pytest

from app import db
from app.crawler.runner import run_workflow
from app.normalize.engine import (
    PARENT_COMPANY,
    normalize_fields,
    read_parent_company,
)
from app.normalize.rules import Rule, RuleConfigError, build_rule
from tests.test_company_selector import (
    WITH_COMPANY,
    WITHOUT_COMPANY,
    make_conn,
    stub_fetcher,
)

# 저장소가 싣고 나가는 규칙 초기값. 손으로 옮겨 적으면 파일이 바뀌어도 테스트는 옛 값을 본다
SEED_RULES = pathlib.Path(__file__).parent.parent / "seeds" / "normalization-rules.json"


def seeded_company_rules() -> list[Rule]:
    """`seeds/normalization-rules.json` 의 `company_name` 규칙 그대로."""
    data = json.loads(SEED_RULES.read_text(encoding="utf-8"))
    return [
        build_rule(row["field_name"], row["rule_type"], row["config"], priority=row["priority"])
        for row in data["rules"]
        if row["field_name"] == "company_name"
    ]


def companies(conn: sqlite3.Connection) -> list[tuple[str | None, str | None]]:
    """(모회사, 자회사) 짝. 순서가 칸의 넓은 쪽부터인 것은 화면과 계약 문서와 같다."""
    rows = conn.execute(
        "SELECT parent_company_name, company_name FROM normalized_jobs ORDER BY raw_job_id"
    ).fetchall()
    return [(row["parent_company_name"], row["company_name"]) for row in rows]


def test_the_parsed_value_and_the_operator_value_no_longer_compete() -> None:
    """두 값이 한 칸을 두고 다투던 자리다. 이제 각자의 칸에 앉는다."""
    fields = normalize_fields({"company_name": "삼성SDS"}, [], "삼성전자")

    assert (fields[PARENT_COMPANY], fields["company_name"]) == ("삼성전자", "삼성SDS")


def test_the_subsidiary_stays_empty_when_the_site_did_not_name_one() -> None:
    """이 Push 의 핵심이다. 모회사 이름이 자회사 칸으로 새어 들어가면 안 된다."""
    for raw in ({}, {"company_name": ""}):
        fields = normalize_fields(raw, [], "삼성전자")

        assert fields[PARENT_COMPANY] == "삼성전자"
        assert fields["company_name"] is None


def test_a_blank_parsed_company_is_the_trim_rule_s_job() -> None:
    """`company_name` 는 이제 다른 필드와 똑같다. 공백만 든 값을 비우는 것은 규칙이 한다.

    해결 단계가 공백을 판정하던 자리가 사라졌다. 그 판정을 여기 남겨 두면 `company_name` 하나만
    다른 필드와 다르게 동작하고, 그 차이는 규칙을 고칠 때 드러난다
    (`seeds/normalization-rules.json` 의 `company_name` trim 규칙이 우선순위 0 이다).
    """
    fields = normalize_fields(
        {"company_name": "   "}, [build_rule("company_name", "trim", {})], "삼성전자"
    )

    assert fields[PARENT_COMPANY] == "삼성전자"
    assert fields["company_name"] is None


def test_neither_column_is_filled_with_an_empty_string() -> None:
    """빈 문자열은 "회사명이 있다" 와 구분되지 않는다. 값 없음은 NULL 하나로만 나타난다."""
    fields = normalize_fields({"company_name": ""}, [], None)
    assert (fields[PARENT_COMPANY], fields["company_name"]) == (None, None)

    blank = normalize_fields({"company_name": ""}, [], "   ")
    assert blank[PARENT_COMPANY] is None


async def test_two_affiliates_on_one_site_get_different_subsidiaries(
    tmp_path: pathlib.Path,
) -> None:
    """사이트 하나에 계열사 공고가 섞여도 공고마다 자회사가 따로 붙고, 모회사는 같다."""
    conn = make_conn(tmp_path / "jobs.db", WITH_COMPANY, default_company="삼성전자")
    try:
        await run_workflow(conn, 1, fetcher=stub_fetcher(), limit=2)

        assert companies(conn) == [("삼성전자", "삼성SDS"), ("삼성전자", "삼성전기(주)")]
    finally:
        conn.close()


async def test_a_site_without_a_company_keeps_the_subsidiary_null(
    tmp_path: pathlib.Path,
) -> None:
    """옛 동작이면 두 건 모두 `삼성전자` 였다. 그것이 계열사를 가르는 값을 지우고 있었다."""
    conn = make_conn(tmp_path / "jobs.db", WITHOUT_COMPANY, default_company="삼성전자")
    try:
        await run_workflow(conn, 1, fetcher=stub_fetcher(), limit=2)

        assert companies(conn) == [("삼성전자", None), ("삼성전자", None)]
    finally:
        conn.close()


async def test_아무것도_안_적으면_모회사도_비어_있다(tmp_path: pathlib.Path) -> None:
    """2026-08-29 결정이 2026-08-26 의 크롤러 이름 대체를 대신한다.

    등록 화면이 이 칸을 필수로 받으므로 실제로는 일어나지 않지만, 화면을 거치지 않고 만든
    행(CLI, 가져오기)까지 대비한다 — 그런 행은 짐작 없이 그냥 비워 둔다.
    """
    conn = make_conn(tmp_path / "jobs.db", WITHOUT_COMPANY)
    try:
        await run_workflow(conn, 1, fetcher=stub_fetcher(), limit=2)

        assert companies(conn) == [(None, None), (None, None)]
    finally:
        conn.close()


def test_비어_있는_모회사는_크롤러_이름으로_대신하지_않는다(tmp_path: pathlib.Path) -> None:
    """2026-08-29 결정. 등록 화면이 이 칸을 필수로 받으므로 짐작할 이유가 없다."""
    conn = _seeded(tmp_path, name="토스", default_company=None, parsed="")
    try:
        assert read_parent_company(conn, 1) is None

        fields = normalize_fields(_raw(""), [], read_parent_company(conn, 1))

        assert fields[PARENT_COMPANY] is None
        assert fields["company_name"] is None
    finally:
        conn.close()


def test_what_the_operator_wrote_beats_the_crawler_name(tmp_path: pathlib.Path) -> None:
    conn = _seeded(tmp_path, name="토스", default_company="비바리퍼블리카", parsed="")
    try:
        assert read_parent_company(conn, 1) == "비바리퍼블리카"
    finally:
        conn.close()


def test_the_parsed_value_never_reaches_the_parent_column(tmp_path: pathlib.Path) -> None:
    """계열사 구분은 파싱값만 할 수 있다. 그 값이 모회사 칸으로 올라가면 구분이 사라진다."""
    conn = _seeded(tmp_path, name="삼성", default_company="삼성전자", parsed="삼성SDS")
    try:
        fields = normalize_fields(_raw("삼성SDS"), [], read_parent_company(conn, 1))

        assert (fields[PARENT_COMPANY], fields["company_name"]) == ("삼성전자", "삼성SDS")
    finally:
        conn.close()


def _raw(company_name: str) -> dict[str, str]:
    return {"title": "공고", "body": "본문", "company_name": company_name}


def _seeded(
    tmp_path: pathlib.Path, *, name: str, default_company: str | None, parsed: str
) -> sqlite3.Connection:
    conn = db.connect(tmp_path / "jobs.db")
    db.migrate_up(conn)
    conn.execute(
        """
        INSERT INTO crawlers (id, name, list_url, status, default_company)
        VALUES (1, ?, 'https://x', 'promoted', ?)
        """,
        (name, default_company),
    )
    conn.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, ?)", (name,))
    conn.execute(
        """
        INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash)
        VALUES (1, 'https://x/1', ?, 'hash1')
        """,
        (json.dumps(_raw(parsed), ensure_ascii=False),),
    )
    return conn


def test_the_seeded_company_rules_still_apply_to_the_subsidiary() -> None:
    """칸을 가르면서 규칙을 옮기지 않았다. 넷은 그대로 자회사에 걸린다."""
    rules = seeded_company_rules()
    assert len(rules) == 4, "seeds 의 `company_name` 규칙이 넷이 아니다"

    fields = normalize_fields({"company_name": "  삼성전기(주)  "}, rules, "삼성전자")

    assert fields["company_name"] == "삼성전기"
    assert fields[PARENT_COMPANY] == "삼성전자"


def test_the_seeded_company_rules_do_not_touch_the_parent() -> None:
    """`현대차` 는 mapping 규칙의 키다. 모회사가 규칙을 탔다면 `현대자동차` 가 됐을 값이다.

    모회사는 운영자가 크롤러에 적어 둔 값을 옮기는 칸이다. 규칙이 거기 걸리면 크롤러 화면에
    적힌 값과 저장된 값이 달라지고, 운영자는 자기가 적은 이름을 어디에서도 찾지 못한다.
    """
    fields = normalize_fields({"company_name": "삼성SDS"}, seeded_company_rules(), "현대차")

    assert fields[PARENT_COMPANY] == "현대차"
    assert fields["company_name"] == "삼성SDS"


def test_a_rule_written_for_the_parent_column_is_refused() -> None:
    """규칙을 태우지 않는 칸이라 `NORMALIZED_FIELDS` 에 없다. 화면도 이 예외로 거절한다."""
    with pytest.raises(RuleConfigError) as caught:
        build_rule(PARENT_COMPANY, "trim", {})

    assert caught.value.reason == "unknown_field"


def test_a_rule_that_empties_the_subsidiary_leaves_the_parent_alone() -> None:
    """자회사를 비우는 규칙이 모회사까지 비우면 두 칸이 도로 하나가 된다."""
    rule = build_rule("company_name", "regex", {"pattern": ".*", "replacement": ""})

    fields = normalize_fields({"company_name": "삼성SDS"}, [rule], "삼성전자")

    assert fields["company_name"] is None
    assert fields[PARENT_COMPANY] == "삼성전자"
