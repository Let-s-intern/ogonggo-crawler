"""공고 하나를 직무마다 나누는 분류 (2026-09-11 결정).

모델을 부르지 않는다. 가짜 응답으로 보는 것은 셋이다 — 공통 조각이 나눈 공고마다 붙는가,
직무 이름과 판정 칸이 공고마다 따로인가, 나눈 공고가 번호마다 저장되는가.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from app import db
from app.classify import store
from app.classify.batch import ClassifyProgress, classify_ids
from app.classify.classifier import ClassificationResult, ClassifyError, classify_body
from app.config import Settings
from tests.classify_fakes import common_body, pieces, posting_body, response
from tests.test_selector_generator import FakeClient

TITLE = "2026년 하반기 R&D 경력사원 채용"
# 줄 번호: [0] 제목, [1] 부터 본문
BODY = "\n".join(
    [
        "■ 공통 자격요건",
        "해외여행에 결격사유가 없는 분",
        "■ 복지",
        "사내 식당 운영",
        "[로봇 SW 개발]",
        "로봇 제어 소프트웨어를 개발합니다",
        "C++ 경력 3년 이상",
        "[비전 AI 연구]",
        "영상 인식 모델을 연구합니다",
        "석사 이상 학위 보유",
    ]
)

SPLIT = response(
    common=common_body(
        qualifications=pieces("해외여행에 결격사유가 없는 분", 2),
        benefits=pieces("사내 식당 운영", 4),
    ),
    postings=[
        posting_body(
            position_name=pieces("로봇 SW 개발", 5),
            responsibilities=pieces("로봇 제어 소프트웨어를 개발합니다", 6),
            qualifications=pieces("C++ 경력 3년 이상", 7),
            experience_type="EXPERIENCED",
            experience_type_evidence="C++ 경력 3년 이상",
        ),
        posting_body(
            position_name=pieces("비전 AI 연구", 8),
            responsibilities=pieces("영상 인식 모델을 연구합니다", 9),
            qualifications=pieces("석사 이상 학위 보유", 10),
            education_level="MASTER",
            education_level_evidence="석사 이상 학위 보유",
        ),
    ],
)


def settings_with_key() -> Settings:
    return Settings(gemini_api_key="테스트키", gemini_model="gemini-3.5-flash")


async def classify(text: str) -> ClassificationResult:
    return await classify_body(
        BODY, title=TITLE, settings=settings_with_key(), client=FakeClient(text)
    )


async def test_공통_조각은_나눈_공고마다_그_공고의_조각_앞에_붙는다() -> None:
    result = await classify(SPLIT)

    first, second = result.postings
    assert first.fields["qualifications"] == "해외여행에 결격사유가 없는 분\nC++ 경력 3년 이상"
    assert second.fields["qualifications"] == "해외여행에 결격사유가 없는 분\n석사 이상 학위 보유"
    assert first.fields["benefits"] == second.fields["benefits"] == "사내 식당 운영"


async def test_직무_이름과_판정_칸은_공고마다_따로다() -> None:
    result = await classify(SPLIT)

    assert result.split
    first, second = result.postings
    assert (first.fields["position_name"], second.fields["position_name"]) == (
        "로봇 SW 개발",
        "비전 AI 연구",
    )
    assert (first.fields["responsibilities"], second.fields["responsibilities"]) == (
        "로봇 제어 소프트웨어를 개발합니다",
        "영상 인식 모델을 연구합니다",
    )
    assert (first.fields["experience_type"], second.fields["experience_type"]) == (
        "EXPERIENCED",
        "",
    )
    assert (first.fields["education_level"], second.fields["education_level"]) == ("", "MASTER")


async def test_직무가_하나인_공고는_나누지_않는다() -> None:
    result = await classify(response(responsibilities="로봇 제어 소프트웨어를 개발합니다"))

    assert not result.split
    assert [posting.fields["responsibilities"] for posting in result.postings] == [
        "로봇 제어 소프트웨어를 개발합니다"
    ]


async def test_공고를_내지_않으면_공통_칸으로_공고_하나를_만든다() -> None:
    """빈 목록을 실패로 보지 않는다. 공통 칸만 있어도 그 공고의 내용이다."""
    text = response(postings=[], common=common_body(benefits=pieces("사내 식당 운영", 4)))

    result = await classify(text)

    assert len(result.postings) == 1
    assert result.postings[0].fields["benefits"] == "사내 식당 운영"


@pytest.mark.parametrize(
    "payload",
    [
        # 이름부터 스키마에 없다. 자리만 틀린 칸과 달리 옮길 곳이 없다
        {"common": {"other": []}, "postings": []},
        {"postings": [{"salary": "협의"}]},
    ],
)
async def test_묶음_안에_스키마에_없는_칸이_오면_거절한다(payload: dict[str, Any]) -> None:
    with pytest.raises(ClassifyError) as caught:
        await classify(json.dumps(payload, ensure_ascii=False))

    assert caught.value.reason == "unknown_field"


async def test_나눈_공고의_메모에는_몇_번_공고인지가_붙는다() -> None:
    """글자를 못 찾아 줄 전체를 남긴 칸이 어느 공고의 것인지 알아야 고친다."""
    text = response(
        postings=[
            posting_body(responsibilities=pieces("원문에 없는 업무", 6)),
            posting_body(responsibilities=pieces("영상 인식 모델을 연구합니다", 9)),
        ]
    )

    result = await classify(text)

    assert any(note.startswith("1번 공고: ") for note in result.notes), result.notes
    assert not any(note.startswith("2번 공고: ") for note in result.notes), result.notes


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (id, name, list_url, status)"
        " VALUES (1, '테스트', 'https://x', 'promoted')"
    )
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, '테스트')")
    raw = {
        "source_url": "https://x/1",
        "title": TITLE,
        "body": BODY,
        "qualifications": "",
        "recruitment_end_at": "",
        "department": "",
        "company_name": "테스트회사",
    }
    connection.execute(
        "INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash)"
        " VALUES (1, ?, ?, 'hash1')",
        (raw["source_url"], json.dumps(raw, ensure_ascii=False)),
    )
    try:
        yield connection
    finally:
        connection.close()


async def classify_stored(conn: sqlite3.Connection, text: str) -> ClassifyProgress:
    progress = ClassifyProgress()
    await classify_ids(conn, [1], progress, client=FakeClient(text), settings=settings_with_key())
    return progress


async def test_나눈_공고는_번호마다_저장되고_직무_이름이_남는다(conn: sqlite3.Connection) -> None:
    progress = await classify_stored(conn, SPLIT)

    assert progress.processed == 1
    rows = conn.execute(
        "SELECT part, part_role, qualifications FROM job_classifications ORDER BY part"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (1, "로봇 SW 개발", "해외여행에 결격사유가 없는 분\nC++ 경력 3년 이상"),
        (2, "비전 AI 연구", "해외여행에 결격사유가 없는 분\n석사 이상 학위 보유"),
    ]


async def test_나누지_않은_공고는_1번_하나이고_직무_이름을_따로_남기지_않는다(
    conn: sqlite3.Connection,
) -> None:
    await classify_stored(conn, response(responsibilities="로봇 제어 소프트웨어를 개발합니다"))

    rows = conn.execute("SELECT part, part_role FROM job_classifications").fetchall()
    assert [tuple(row) for row in rows] == [(1, None)]


def test_분류_행이_여럿인_공고도_범위에서_한_번만_나온다(conn: sqlite3.Connection) -> None:
    """같은 수집 건이 두 번 나오면 한 실행이 같은 공고를 두 번 부른다."""
    conn.executemany(
        "INSERT INTO job_classifications (raw_job_id, part, model) VALUES (1, ?, 'model')",
        [(1,), (2,)],
    )

    assert store.scope_ids(conn, store.EMPTY_FIELDS) == [1]
    assert store.scope_count(conn, store.EMPTY_FIELDS) == 1


async def test_공통_묶음에_온_판정_칸은_공고마다_옮겨_받는다() -> None:
    """DeepSeek 가 판정 칸을 common 에 넣는다 (2026-09-18). 이름은 맞고 자리만 틀렸다.

    공고 안에 값이 있으면 그 값이 먼저다 — 직무마다 다를 수 있는 칸이다.
    """
    payload = json.loads(SPLIT)
    payload["common"]["employment_type"] = "FULL_TIME"
    payload["common"]["experience_type"] = "NEWCOMER"

    result = await classify(json.dumps(payload, ensure_ascii=False))

    assert [posting.fields["employment_type"] for posting in result.postings] == [
        "FULL_TIME",
        "FULL_TIME",
    ]
    # 첫 공고는 자기 값(EXPERIENCED)을 냈고, 둘째는 비워 둬서 공통 값을 받는다
    assert [posting.fields["experience_type"] for posting in result.postings] == [
        "EXPERIENCED",
        "NEWCOMER",
    ]


async def test_응답_맨_위에_온_판정_칸도_옮겨_받는다() -> None:
    payload = json.loads(SPLIT)
    payload["employment_type"] = "FULL_TIME"

    result = await classify(json.dumps(payload, ensure_ascii=False))

    assert {posting.fields["employment_type"] for posting in result.postings} == {"FULL_TIME"}


async def test_공고가_없고_공통_묶음에만_칸이_있으면_공고_하나로_받는다() -> None:
    """공고를 내지 않고 전부 common 에 담은 답이다. 직무 이름도 조각째 옮긴다."""
    payload = {
        "common": {
            **common_body(benefits=pieces("사내 식당 운영", 4)),
            "position_name": pieces("로봇 SW 개발", 5),
            "employment_type": "FULL_TIME",
        },
        "postings": [],
    }

    result = await classify(json.dumps(payload, ensure_ascii=False))

    assert len(result.postings) == 1
    assert result.postings[0].fields["position_name"] == "로봇 SW 개발"
    assert result.postings[0].fields["employment_type"] == "FULL_TIME"
    assert result.postings[0].fields["benefits"] == "사내 식당 운영"


async def test_다시_물을_때_앞_답이_거절된_이유를_붙인다() -> None:
    """같은 프롬프트를 그대로 다시 보내면 모델은 무엇이 틀렸는지 모른다."""
    broken = json.dumps({"common": {"other": []}, "postings": []})
    client = FakeClient(broken, SPLIT)

    await classify_body(BODY, title=TITLE, settings=settings_with_key(), client=client)

    first, second = (call["contents"] for call in client.calls)
    assert "앞 답이 거절됐다" not in first
    assert "앞 답이 거절됐다" in second
    assert "other" in second
