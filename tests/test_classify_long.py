"""긴 공고는 짜임을 먼저 묻고 직무마다 나눠 부른다 (2026-09-11 결정).

모델을 부르지 않는다. 가짜 클라이언트는 받은 차례대로 답한다 — 첫 답이 짜임이고, 그다음이
직무마다의 분류다.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from app import db
from app.classify.batch import ClassifyProgress, classify_ids
from app.classify.classifier import (
    MAX_BODY_CHARS,
    MAX_OUTLINE_CHARS,
    build_outline_prompt,
    build_part_prompt,
    build_prompt,
    classify_body,
)
from app.classify.pieces import number_lines, to_ranges
from app.classify.schema import ClassifySchemaError, Outline, parse_outline
from app.config import Settings
from tests.classify_fakes import pieces, response
from tests.test_selector_generator import FakeClient

TITLE = "2026년 하반기 신입사원 채용"
# 어느 직무의 내용도 공통 안내도 아닌 글. 직무 호출에 실리면 안 된다
INTERVIEW = "선배 사원 인터뷰 문장입니다 " * 20
# 줄 번호: [0] 제목, [1] 부터 본문. 인터뷰가 [7] 부터 붙어 글이 상한을 넘는다
BODY = "\n".join(
    [
        "■ 공통 전형 절차",
        "서류전형 → 면접 → 입사",
        "[기계]",
        "설비 설계 업무를 합니다",
        "[HR]",
        "인사 제도를 운영합니다",
        *(f"{INTERVIEW}{index}" for index in range(45)),
    ]
)

OUTLINE = json.dumps(
    {
        "roles": [
            {"lines": [{"start": 3, "end": 4}]},
            {"lines": [{"start": 5, "end": 6}]},
        ],
        "common_lines": [{"start": 1, "end": 2}],
    }
)
MECHANICAL = response(
    job_role=pieces("기계", 3),
    duties=pieces("설비 설계 업무를 합니다", 4),
    hiring_process=pieces("서류전형 → 면접 → 입사", 2),
)
HR = response(
    job_role=pieces("HR", 5),
    duties=pieces("인사 제도를 운영합니다", 6),
    hiring_process=pieces("서류전형 → 면접 → 입사", 2),
)


def settings_with_key() -> Settings:
    return Settings(gemini_api_key="테스트키", gemini_model="gemini-3.5-flash")


def test_이_본문은_한_번에_보내는_상한을_넘는다() -> None:
    assert MAX_BODY_CHARS < len(BODY) < MAX_OUTLINE_CHARS


async def test_긴_공고는_짜임을_묻고_직무마다_부른다() -> None:
    client = FakeClient(OUTLINE, MECHANICAL, HR)

    result = await classify_body(BODY, title=TITLE, settings=settings_with_key(), client=client)

    assert len(client.calls) == 3
    assert client.calls[0]["config"]["response_schema"] is Outline
    assert result.split
    assert [posting.fields["job_role"] for posting in result.postings] == ["기계", "HR"]
    assert [posting.fields["duties"] for posting in result.postings] == [
        "설비 설계 업무를 합니다",
        "인사 제도를 운영합니다",
    ]
    assert [posting.sent_lines for posting in result.postings] == [(1, 2, 3, 4), (1, 2, 5, 6)]


async def test_직무마다_보내는_글에는_공통_줄과_그_직무의_줄만_있다() -> None:
    client = FakeClient(OUTLINE, MECHANICAL, HR)

    await classify_body(BODY, title=TITLE, settings=settings_with_key(), client=client)

    outline = client.calls[0]["contents"]
    assert f"[7] {INTERVIEW}0" in outline
    mechanical = client.calls[1]["contents"]
    assert f"[0] {TITLE}" in mechanical
    assert "[1] ■ 공통 전형 절차" in mechanical
    assert "[3] [기계]" in mechanical
    assert "[5] [HR]" not in mechanical
    assert INTERVIEW not in mechanical
    assert "긴 공고의 한 직무" in mechanical


async def test_비용과_물은_횟수는_모든_호출을_더한다() -> None:
    client = FakeClient(OUTLINE, MECHANICAL, HR)
    seen: list[int] = []

    result = await classify_body(
        BODY,
        title=TITLE,
        settings=settings_with_key(),
        client=client,
        on_call=lambda usage: seen.append(usage.total_tokens),
    )

    assert len(seen) == 3
    assert result.usage.total_tokens == sum(seen)
    assert result.attempts == 3


async def test_짜임에서_직무를_못_읽으면_잘라서_한_번에_나눈다() -> None:
    client = FakeClient(
        json.dumps({"roles": [], "common_lines": []}),
        response(duties="설비 설계 업무를 합니다"),
    )

    result = await classify_body(BODY, title=TITLE, settings=settings_with_key(), client=client)

    assert len(client.calls) == 2
    assert [posting.fields["duties"] for posting in result.postings] == ["설비 설계 업무를 합니다"]
    assert result.postings[0].sent_lines == ()
    assert any(str(MAX_BODY_CHARS) in note for note in result.notes), result.notes


async def test_짜임을_물을_때도_상한을_넘는_글은_자른다() -> None:
    long_body = BODY + "\n" + "\n".join("나" * 100 for _ in range(500))
    client = FakeClient(OUTLINE, MECHANICAL, HR)

    result = await classify_body(
        long_body, title=TITLE, settings=settings_with_key(), client=client
    )

    assert len(client.calls[0]["contents"]) < len(long_body)
    assert any(str(MAX_OUTLINE_CHARS) in note for note in result.notes), result.notes


def test_짜임의_줄_번호는_글_안의_것만_남는다() -> None:
    """번호가 틀린 범위 하나로 공고 전체를 실패로 만들지 않는다. 제목(0번)은 늘 붙는다."""
    text = json.dumps(
        {
            "roles": [
                {"lines": [{"start": 5, "end": 3}, {"start": 90, "end": 99}]},
                {"lines": []},
            ],
            "common_lines": [{"start": 0, "end": 1}],
        }
    )

    parsed = parse_outline(text, line_count=10)

    assert parsed.roles == [[3, 4, 5]]
    assert parsed.common == [1]


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"postings": []}, "unknown_field"),
        ({"roles": [{"lines": [{"start": "3", "end": 4}]}]}, "unparsable"),
    ],
)
def test_짜임의_모양이_틀리면_거절한다(payload: dict[str, object], reason: str) -> None:
    with pytest.raises(ClassifySchemaError) as caught:
        parse_outline(json.dumps(payload), line_count=10)

    assert caught.value.reason == reason


def test_줄_번호는_이어진_범위로_묶인다() -> None:
    assert to_ranges([7, 1, 2, 3, 2]) == [[1, 3], [7, 7]]


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (id, name, list_url, status)"
        " VALUES (1, '테스트', 'https://x', 'promoted')"
    )
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, '테스트')")
    raw = {"source_url": "https://x/1", "title": TITLE, "body": BODY, "company": "테스트회사"}
    connection.execute(
        "INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash)"
        " VALUES (1, ?, ?, 'hash1')",
        (raw["source_url"], json.dumps(raw, ensure_ascii=False)),
    )
    try:
        yield connection
    finally:
        connection.close()


async def test_직무마다_보낸_줄이_분류_행에_남는다(conn: sqlite3.Connection) -> None:
    """다시 분류할 때 같은 줄을 보내 나눈 목록을 고정하려고 남긴다."""
    progress = ClassifyProgress()

    await classify_ids(
        conn,
        [1],
        progress,
        client=FakeClient(OUTLINE, MECHANICAL, HR),
        settings=settings_with_key(),
    )

    assert progress.processed == 1
    rows = conn.execute(
        "SELECT part, part_role, part_lines FROM job_classifications ORDER BY part"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (1, "기계", "[[1, 4]]"),
        (2, "HR", "[[1, 2], [5, 6]]"),
    ]


def test_세_프롬프트_모두_조직_아래_직무면_조직_이름_줄도_함께_보내라고_한다() -> None:
    """한 번에 나눌 때도, 짜임을 물을 때도, 직무마다 부를 때도 같은 규칙이어야 제목이 안 겹친다."""
    prompt, _ = build_prompt(BODY[:1000], TITLE)
    outline, _ = build_outline_prompt(BODY, TITLE)
    part = build_part_prompt(number_lines(TITLE, BODY), [1, 2, 3, 4])

    for text in (prompt, outline, part):
        assert "조직 이름이 적힌 줄" in text
