"""분류 응답 스키마가 `job_taxonomy` 표를 따라 동적으로 만들어지는지 (2.2.V).

`Classification`(정적 모델)은 손대지 않는다. `build_classification_model()` 이 호출
시점에 표의 켜진 값으로 `job_field`/`job_role` 를 공고 모델에 더하고, 그 공고를 담는 응답
모델을 새로 만든다. 두 칸은 공고마다 고르는 칸이다.
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator
from typing import get_args

import pytest

from app import db, taxonomy
from app.classify.schema import Classification, build_classification_model, posting_model_of


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    try:
        yield connection
    finally:
        connection.close()


def test_표가_비어있으면_기본_모델_그대로다(conn: sqlite3.Connection) -> None:
    model = build_classification_model(conn)

    assert model is Classification
    assert "job_field" not in posting_model_of(model).model_fields


def test_대분류_소분류가_있으면_그_이름이_enum이_된다(conn: sqlite3.Connection) -> None:
    major = taxonomy.create(conn, parent_id=None, name="IT·개발")
    taxonomy.create(conn, parent_id=major.id, name="서버·백엔드")
    taxonomy.create(conn, parent_id=major.id, name="프론트엔드")
    other = taxonomy.create(conn, parent_id=None, name="AI·데이터")
    taxonomy.create(conn, parent_id=other.id, name="데이터 엔지니어")

    model = build_classification_model(conn)
    posting = posting_model_of(model)

    major_choices = set(get_args(posting.model_fields["job_field"].annotation))
    minor_choices = set(get_args(posting.model_fields["job_role"].annotation))
    assert major_choices == {"IT·개발", "AI·데이터", "판단불가"}
    assert minor_choices == {"서버·백엔드", "프론트엔드", "데이터 엔지니어", "판단불가"}
    assert issubclass(model, Classification)


def test_꺼진_값은_목록에서_빠진다(conn: sqlite3.Connection) -> None:
    major = taxonomy.create(conn, parent_id=None, name="IT·개발")
    on = taxonomy.create(conn, parent_id=major.id, name="서버·백엔드")
    off = taxonomy.create(conn, parent_id=major.id, name="프론트엔드")
    taxonomy.set_enabled(conn, off.id, False)

    posting = posting_model_of(build_classification_model(conn))

    minor_choices = set(get_args(posting.model_fields["job_role"].annotation))
    assert on.name in minor_choices
    assert off.name not in minor_choices


def test_대분류만_있고_켜진_소분류가_없으면_소분류_필드가_없다(conn: sqlite3.Connection) -> None:
    major = taxonomy.create(conn, parent_id=None, name="공공·복지")

    posting = posting_model_of(build_classification_model(conn))

    assert "job_field" in posting.model_fields
    assert "job_role" not in posting.model_fields
    assert major.name in get_args(posting.model_fields["job_field"].annotation)


def test_모델은_직접_만들_수_있고_기존_아홉_칸도_그대로_있다(conn: sqlite3.Connection) -> None:
    taxonomy.create(conn, parent_id=None, name="IT·개발")

    model = build_classification_model(conn)
    instance = model.model_validate(
        {"postings": [{"job_field": "IT·개발", "experience_type": "경력"}]}
    )

    assert instance.postings[0].job_field == "IT·개발"
    assert instance.postings[0].experience_type == "경력"
