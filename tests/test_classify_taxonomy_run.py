"""분류 실행이 직무 분류를 실제로 채우는지 (3.1.V, 3.2.V, 3.3.V).

`FakeClient` 로 도니 Gemini 를 부르지 않는다. 표는 실제 `job_taxonomy` 에 넣고
`app.taxonomy.enabled_tree()`/`build_classification_model()` 로 만든 진짜 트리·모델을
쓴다 — 배치가 실제로 조립하는 것과 같은 값이어야 근거 검사·저장 경로까지 같이 본다.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from app import db, taxonomy
from app.classify.classifier import classify_body
from app.classify.schema import RESPONSE_FIELDS, build_classification_model
from app.config import Settings
from tests.classify_fakes import response_body
from tests.test_selector_generator import FakeClient

BODY = "당근마켓에서 서버 개발자를 찾습니다. 백엔드 API 를 설계하고 운영합니다."


def settings_with_key() -> Settings:
    return Settings(gemini_api_key="테스트키", gemini_model="gemini-3.5-flash")


def response(**fields: str) -> str:
    base = response_body(**fields)
    # 직무 분류는 공고마다 고르는 칸이라 공고 안에 앉는다
    base["postings"][0].update(
        {
            "job_field": fields.get("job_field", ""),
            "job_field_evidence": fields.get("job_field_evidence", ""),
            "job_role": fields.get("job_role", ""),
            "job_role_evidence": fields.get("job_role_evidence", ""),
        }
    )
    return json.dumps(base, ensure_ascii=False)


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    major = taxonomy.create(connection, parent_id=None, name="IT·개발")
    taxonomy.create(connection, parent_id=major.id, name="서버·백엔드")
    taxonomy.create(connection, parent_id=major.id, name="프론트엔드")
    taxonomy.create(connection, parent_id=None, name="영업")
    try:
        yield connection
    finally:
        connection.close()


async def test_대분류_소분류를_고르면_결과에_채워진다(conn: sqlite3.Connection) -> None:
    tree = taxonomy.enabled_tree(conn)
    model = build_classification_model(conn)
    text = response(
        job_field="IT·개발",
        job_field_evidence="서버 개발자를 찾습니다",
        job_role="서버·백엔드",
        job_role_evidence="백엔드 API 를 설계하고 운영합니다",
    )

    result = await classify_body(
        BODY,
        title="서버 개발자 채용",
        taxonomy_tree=tree,
        response_model=model,
        settings=settings_with_key(),
        client=FakeClient(text),
    )

    assert result.postings[0].fields["job_field"] == "IT·개발"
    assert result.postings[0].fields["job_role"] == "서버·백엔드"
    assert result.postings[0].dropped == []


async def test_프롬프트에_트리가_한번에_들어간다(conn: sqlite3.Connection) -> None:
    """두 단계로 나눠 묻지 않는다 — 대분류·소분류가 같은 호출 프롬프트에 함께 있다."""
    tree = taxonomy.enabled_tree(conn)
    model = build_classification_model(conn)
    client = FakeClient(response(job_field="IT·개발", job_role="서버·백엔드"))

    await classify_body(
        BODY,
        title="서버 개발자 채용",
        taxonomy_tree=tree,
        response_model=model,
        settings=settings_with_key(),
        client=client,
    )

    prompt = client.calls[0]["contents"]
    assert "IT·개발: 서버·백엔드, 프론트엔드" in prompt
    assert "영업" in prompt


async def test_근거가_원문에_없으면_버려진다(conn: sqlite3.Connection) -> None:
    tree = taxonomy.enabled_tree(conn)
    model = build_classification_model(conn)
    text = response(
        job_field="IT·개발",
        job_field_evidence="본문에 없는 문장입니다",
        job_role="서버·백엔드",
        job_role_evidence="이것도 본문에 없다",
    )

    result = await classify_body(
        BODY,
        title="서버 개발자 채용",
        taxonomy_tree=tree,
        response_model=model,
        settings=settings_with_key(),
        client=FakeClient(text),
    )

    assert result.postings[0].fields.get("job_field", "") == ""
    assert result.postings[0].fields.get("job_role", "") == ""
    assert set(result.postings[0].dropped) == {"job_field", "job_role"}


async def test_대분류만_정해지고_소분류는_판단불가면_대분류만_남는다(
    conn: sqlite3.Connection,
) -> None:
    tree = taxonomy.enabled_tree(conn)
    model = build_classification_model(conn)
    text = response(
        job_field="IT·개발",
        job_field_evidence="서버 개발자를 찾습니다",
        job_role="판단불가",
    )

    result = await classify_body(
        BODY,
        title="서버 개발자 채용",
        taxonomy_tree=tree,
        response_model=model,
        settings=settings_with_key(),
        client=FakeClient(text),
    )

    assert result.postings[0].fields["job_field"] == "IT·개발"
    assert result.postings[0].fields.get("job_role", "") == ""
    assert result.postings[0].dropped == []


async def test_소분류_근거만_없으면_대분류는_그대로_남는다(conn: sqlite3.Connection) -> None:
    """소분류가 근거 검사에서 버려져도 대분류까지 같이 버리지 않는다."""
    tree = taxonomy.enabled_tree(conn)
    model = build_classification_model(conn)
    text = response(
        job_field="IT·개발",
        job_field_evidence="서버 개발자를 찾습니다",
        job_role="서버·백엔드",
        job_role_evidence="본문에 없는 근거",
    )

    result = await classify_body(
        BODY,
        title="서버 개발자 채용",
        taxonomy_tree=tree,
        response_model=model,
        settings=settings_with_key(),
        client=FakeClient(text),
    )

    assert result.postings[0].fields["job_field"] == "IT·개발"
    assert result.postings[0].fields.get("job_role", "") == ""
    assert result.postings[0].dropped == ["job_role"]


async def test_대분류가_판단불가면_소분류도_비운다(conn: sqlite3.Connection) -> None:
    """대분류 없이 소분류만 있는 상태는 만들지 않는다."""
    tree = taxonomy.enabled_tree(conn)
    model = build_classification_model(conn)
    text = response(
        job_field="판단불가",
        job_role="서버·백엔드",
        job_role_evidence="백엔드 API 를 설계하고 운영합니다",
    )

    result = await classify_body(
        BODY,
        title="서버 개발자 채용",
        taxonomy_tree=tree,
        response_model=model,
        settings=settings_with_key(),
        client=FakeClient(text),
    )

    assert result.postings[0].fields.get("job_field", "") == ""
    assert result.postings[0].fields.get("job_role", "") == ""


async def test_표가_비어있으면_직무_분류를_묻지_않는다() -> None:
    """taxonomy_tree 를 안 주면 옛 아홉 칸짜리 호출과 똑같다."""
    text = json.dumps({name: "" for name in RESPONSE_FIELDS})

    result = await classify_body(
        BODY,
        title="서버 개발자 채용",
        settings=settings_with_key(),
        client=FakeClient(text),
    )

    assert "job_field" not in result.postings[0].fields
    assert "job_role" not in result.postings[0].fields
