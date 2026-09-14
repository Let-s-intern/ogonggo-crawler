"""분류가 산업 분류표에서 공고마다 산업을 고른다 (2026-09-14 결정).

`FakeClient` 로 돌아 모델을 부르지 않는다. 산업은 실제 `industries` 표에 넣고
`build_classification_model()`/`industries.enabled_names()` 로 만든 진짜 모델·목록을 쓴다.

| 확인 | 깨지면 |
|---|---|
| 켜진 산업이 응답 스키마의 enum 이 되고 반드시 고른다 | 목록 밖 산업이 저장된다 |
| 직무 분류 표가 비어도 산업은 묻고, 산업 표가 비면 묻지 않는다 | 한 표 때문에 다른 칸이 빈다 |
| 프롬프트에 산업 목록과 규칙이 실린다 | 모델이 목록을 모르고 지어낸다 |
| 근거가 없어도 고른 산업은 남고, 목록 밖이면 버린다 | 오공고로 보낼 산업이 비거나 틀린다 |
| 배치가 산업을 분류 결과와 정규화까지 옮긴다 | 분류한 산업이 저장되지 않는다 |
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Any, get_args

import pytest

from app import db, industries
from app.classify.batch import ClassifyProgress, classify_pending
from app.classify.classifier import classify_body
from app.classify.grounding import NOT_IN_LIST
from app.classify.schema import Classification, build_classification_model, posting_model_of
from app.classify.store import read_classification
from tests.classify_fakes import response_body
from tests.test_classify_run import _seed, settings_with_key
from tests.test_selector_generator import FakeClient

BODY = "모바일 뱅킹 서비스를 운영합니다. 결제 서버를 개발합니다."


def response(**fields: Any) -> str:
    base = response_body(**fields)
    # 산업은 공고마다 고르는 칸이라 공고 안에 앉는다
    base["postings"][0].update(
        {
            "industry": fields.get("industry", ""),
            "industry_evidence": fields.get("industry_evidence", ""),
        }
    )
    return json.dumps(base, ensure_ascii=False)


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    industries.create(connection, name="IT·정보통신업", sort_order=0)
    industries.create(connection, name="금융·은행업", sort_order=1)
    try:
        yield connection
    finally:
        connection.close()


async def classify(conn: sqlite3.Connection, text: str) -> tuple[Any, FakeClient]:
    client = FakeClient(text)
    result = await classify_body(
        BODY,
        title="결제 서버 개발자",
        response_model=build_classification_model(conn),
        industries=industries.enabled_names(conn),
        settings=settings_with_key(),
        client=client,
    )
    return result, client


def test_켜진_산업이_enum이고_반드시_고른다(conn: sqlite3.Connection) -> None:
    posting = posting_model_of(build_classification_model(conn))

    assert set(get_args(posting.model_fields["industry"].annotation)) == {
        "IT·정보통신업",
        "금융·은행업",
    }
    assert posting.model_fields["industry"].is_required()
    # 직무 분류 표는 비어 있다. 그래도 산업은 묻는다
    assert "job_field" not in posting.model_fields


def test_산업_표가_비면_산업을_묻지_않는다(tmp_path: pathlib.Path) -> None:
    connection = db.connect(tmp_path / "empty.db")
    db.migrate_up(connection)
    try:
        assert build_classification_model(connection) is Classification
    finally:
        connection.close()


async def test_프롬프트에_산업_목록과_규칙이_실린다(conn: sqlite3.Connection) -> None:
    _, client = await classify(conn, response(industry="금융·은행업"))

    prompt = client.calls[0]["contents"]
    assert "# 산업 — 아래 목록에서만 고른다" in prompt
    assert "- 금융·은행업" in prompt
    assert "- industry: 산업." in prompt


async def test_근거가_없어도_고른_산업은_남고_목록_밖이면_버린다(conn: sqlite3.Connection) -> None:
    kept, _ = await classify(
        conn, response(industry="금융·은행업", industry_evidence="본문에 없는 문장")
    )
    outside, _ = await classify(conn, response(industry="게임업"))

    assert kept.postings[0].fields["industry"] == "금융·은행업"
    assert kept.postings[0].evidence == {}
    assert outside.postings[0].fields["industry"] == ""
    assert outside.postings[0].reasons["industry"] == NOT_IN_LIST


async def test_배치가_산업을_분류_결과와_정규화까지_옮긴다(conn: sqlite3.Connection) -> None:
    _seed(conn, count=1)
    text = response(
        responsibilities="제휴사 데이터 연동 구조 기획",
        industry="IT·정보통신업",
        industry_evidence="제휴사 데이터 연동 구조 기획",
    )

    await classify_pending(
        conn, ClassifyProgress(), client=FakeClient(text), settings=settings_with_key()
    )

    assert read_classification(conn, 1)["industry"] == "IT·정보통신업"
    row = conn.execute("SELECT industry FROM normalized_jobs WHERE raw_job_id = 1").fetchone()
    assert row["industry"] == "IT·정보통신업"
