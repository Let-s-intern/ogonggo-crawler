"""분류가 무엇을 읽고, 무엇에 근거를 돌려 보고, 어디서 자르고, 무엇을 대상으로 삼는지 본다.

9.1.V ~ 9.4.V 다.

Push 8 이 상세 원문을 `raw_jobs.raw_data_json.source_text` 에 넣었다. 분류는 그것을 읽고,
없으면 본문으로 떨어진다. **폴백이 검사의 요점이다** — 원문은 2026-08-28 이후 수집분에만
있고, 그 전에 쌓인 건에는 키가 아예 없다. 폴백이 없으면 그 공고들이 분류에서 통째로 사라진다
(`.claude/tasks/todo/prd-side-workflows.md` 4절).

근거 검사도 같은 값에 돈다. 원문으로 물어 놓고 본문에 돌려 보면 본문 밖 이름표에서 옳게
뽑은 칸이 통째로 버려진다 (`app/classify/grounding.py`).

제공자를 실제로 부르지 않는다. 어느 값이 갔는지는 가짜 클라이언트가 받은 프롬프트로 본다.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from app import db
from app.classify.batch import ClassifyProgress, classify_ids
from app.classify.classifier import MAX_BODY_CHARS, classify_body
from app.classify.grounding import NO_EVIDENCE, NOT_IN_SOURCE, ground
from app.classify.store import (
    pending_count,
    pending_ids,
    read_classification,
    read_source,
)
from app.config import Settings
from tests.classify_fakes import response
from tests.test_selector_generator import FakeClient
from tests.test_source_text import HTML_DETAIL, parsed

# 본문. 두 건이 같은 본문을 갖는다
BODY = "◆ 업무내용\n제휴사 데이터 연동 구조 기획\n\n◆ 지원자격\n관련 경험 5년 이상이신 분\n"

# 원문에만 있는 줄. 본문 셀렉터 바깥의 이름표 값이고, 실제로 SK·롯데그룹·네이버·카카오·
# 우아한형제들의 조상 1단계가 담은 것이 이것이다 (`.claude/site-recipes/source-text-container.md`)
ONLY_IN_SOURCE = "근무지 성남시 분당구 판교로 235"

# 판정 칸의 근거도 본문 밖에 있을 수 있다. 고용형태가 이름표에만 적힌 사이트가 그렇다
EVIDENCE_IN_SOURCE = "고용 형태 정규직"

SOURCE = f"{ONLY_IN_SOURCE}\n{EVIDENCE_IN_SOURCE}\n{BODY}"


def settings_with_key() -> Settings:
    return Settings(gemini_api_key="테스트키", gemini_model="gemini-3.5-flash")


ANSWER = response(responsibilities="제휴사 데이터 연동 구조 기획")


def insert(conn: sqlite3.Connection, raw_job_id: int, **raw: str) -> None:
    conn.execute(
        """
        INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)
        VALUES (?, 1, ?, ?, ?)
        """,
        (
            raw_job_id,
            f"https://example.test/{raw_job_id}",
            json.dumps(raw, ensure_ascii=False),
            f"hash{raw_job_id}",
        ),
    )


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    """원문이 있는 건 하나와, 원문 키가 아예 없는 옛 건 하나."""
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        """
        INSERT INTO crawlers (id, name, list_url, status)
        VALUES (1, '테스트', 'https://example.test', 'promoted')
        """
    )
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, '테스트')")
    insert(connection, 1, title="공고 1", body=BODY, source_text=SOURCE)
    insert(connection, 2, title="공고 2", body=BODY)
    try:
        yield connection
    finally:
        connection.close()


def test_원문이_있으면_원문을_읽는다(conn: sqlite3.Connection) -> None:
    assert read_source(conn, 1) == SOURCE


def test_원문이_없으면_본문으로_떨어진다(conn: sqlite3.Connection) -> None:
    """옛 건에는 `source_text` 키가 아예 없다. 여기서 빈 값이 나오면 그 공고는 분류를 못 받는다."""
    assert read_source(conn, 2) == BODY


def test_원문이_빈_문자열이어도_본문으로_떨어진다(conn: sqlite3.Connection) -> None:
    """키는 있는데 값이 빈 경우다. 수집이 원문을 못 뽑으면 이 모양이 될 수 있다."""
    insert(conn, 3, title="공고 3", body=BODY, source_text="")

    assert read_source(conn, 3) == BODY


async def test_실행이_보내는_값도_원문과_본문으로_갈린다(conn: sqlite3.Connection) -> None:
    """읽기만 바꾸고 실행이 옛 함수를 계속 부르면 아무것도 달라지지 않는다."""
    client = FakeClient(ANSWER)

    await classify_ids(
        conn, [1, 2], ClassifyProgress(), client=client, settings=settings_with_key()
    )

    첫_건, 둘째_건 = client.calls[0]["contents"], client.calls[1]["contents"]
    assert ONLY_IN_SOURCE in 첫_건
    assert ONLY_IN_SOURCE not in 둘째_건
    assert "제휴사 데이터 연동 구조 기획" in 둘째_건


def test_본문_밖에만_있는_값은_원문에_돌려_보면_산다() -> None:
    """9.2 의 요점이다. 원문으로 물어 놓고 본문에 돌려 보면 이 칸이 버려진다."""
    kept = ground({"region": ONLY_IN_SOURCE}, SOURCE, "공고 1")

    assert kept.fields["region"] == ONLY_IN_SOURCE
    assert kept.dropped == []


def test_같은_값을_본문에_돌려_보면_버려진다() -> None:
    """두 값이 어긋났을 때 무엇을 잃는지 고정한다."""
    dropped = ground({"region": ONLY_IN_SOURCE}, BODY, "공고 1")

    assert dropped.fields["region"] == ""
    assert dropped.reasons["region"] == NOT_IN_SOURCE


def test_판정_칸의_근거_문장도_원문에서_찾는다() -> None:
    kept = ground(
        {"employment_type": "정규직", "employment_type_evidence": EVIDENCE_IN_SOURCE},
        SOURCE,
        "공고 1",
    )

    assert kept.fields["employment_type"] == "정규직"
    assert kept.evidence["employment_type"] == EVIDENCE_IN_SOURCE

    본문뿐 = ground(
        {"employment_type": "정규직", "employment_type_evidence": EVIDENCE_IN_SOURCE},
        BODY,
        "공고 1",
    )
    assert 본문뿐.fields["employment_type"] == ""
    assert 본문뿐.reasons["employment_type"] == NO_EVIDENCE


async def test_실행이_원문에서_뽑은_칸을_버리지_않는다(conn: sqlite3.Connection) -> None:
    """읽는 값과 돌려 보는 값이 갈리면 여기서 잡힌다. 같은 응답을 두 건에 준다."""
    답 = response(
        region=ONLY_IN_SOURCE,
        employment_type="정규직",
        employment_type_evidence=EVIDENCE_IN_SOURCE,
    )

    await classify_ids(
        conn, [1, 2], ClassifyProgress(), client=FakeClient(답), settings=settings_with_key()
    )

    원문_있는_건 = read_classification(conn, 1)
    assert 원문_있는_건["region"] == ONLY_IN_SOURCE
    assert 원문_있는_건["employment_type"] == "정규직"

    # 원문이 없는 건은 지금까지와 같다. 본문에 없는 값은 여전히 버려진다
    본문뿐인_건 = read_classification(conn, 2)
    assert 본문뿐인_건["region"] == ""
    assert 본문뿐인_건["employment_type"] == ""


def test_상한을_넘는_원문은_잘리고_그_사실이_남는다() -> None:
    """자른 것으로 무엇을 놓쳤는지는 응답을 보는 사람이 알아야 한다.

    상한을 넘는 글은 짜임부터 묻는다. 짜임에서 직무를 못 읽으면 앞부분만 보내 한 번에 나눈다
    (`app/classify/classifier.py` 의 `_classify_long`).
    """
    긴_원문 = BODY + "가" * MAX_BODY_CHARS
    직무를_못_읽은_짜임 = json.dumps({"roles": [], "common_lines": []})
    client = FakeClient(직무를_못_읽은_짜임, ANSWER)

    result = asyncio.run(
        classify_body(긴_원문, title="공고 1", settings=settings_with_key(), client=client)
    )

    보낸_글 = client.calls[1]["contents"]
    assert "가" * MAX_BODY_CHARS not in 보낸_글
    assert any(
        str(len(긴_원문)) in note and str(MAX_BODY_CHARS) in note for note in result.notes
    ), result.notes


def test_상한은_잰_원문_전부를_담는다() -> None:
    """9.3 의 결정을 고정한다. 상한을 내리면 여기서 어느 사이트가 잘리는지 바로 나온다.

    2026-08-28 측정에서 원문이 가장 긴 곳은 토스 10,312자다
    (`.claude/site-recipes/source-text-container.md`).
    """
    길이 = {site: len(parsed(site).source_text) for site in HTML_DETAIL}

    assert max(길이.values()) <= MAX_BODY_CHARS, 길이


def test_원문만_있고_본문이_빈_건도_대상이다(conn: sqlite3.Connection) -> None:
    """대상 조건이 본문만 보면 이 건이 조용히 빠진다. 보낼 글이 있는데 영영 안 돈다."""
    insert(conn, 3, title="공고 3", body="", source_text=SOURCE)

    assert pending_ids(conn) == [3, 2, 1]
    assert pending_count(conn) == 3


def test_원문도_본문도_없으면_대상이_아니다(conn: sqlite3.Connection) -> None:
    """나눌 것이 없는 건까지 부르면 그 호출은 통째로 버리는 돈이다."""
    insert(conn, 4, title="공고 4")

    assert 4 not in pending_ids(conn)
    assert pending_count(conn) == 2
