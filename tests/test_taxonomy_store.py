"""직무 분류 저장소 (1.2.V, 1.3.V, 1.4.V).

`app/companies.py` 와 같은 자리다 — 표 하나, 읽기는 예외를 던지지 않고 쓰기는 검증을 지난다.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from app import db, taxonomy

SEED = pathlib.Path(__file__).parent.parent / "seeds" / "job-taxonomy-ogonggo-20261019.json"


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    try:
        yield connection
    finally:
        connection.close()


def test_대분류를_만들고_읽으면_그대로다(conn: sqlite3.Connection) -> None:
    node = taxonomy.create(conn, parent_id=None, name="IT·개발", note="씨앗")

    stored = taxonomy.read(conn, node.id)

    assert stored is not None
    assert (stored.parent_id, stored.name, stored.enabled, stored.note) == (
        None,
        "IT·개발",
        True,
        "씨앗",
    )


def test_소분류는_대분류_밑에_달린다(conn: sqlite3.Connection) -> None:
    major = taxonomy.create(conn, parent_id=None, name="IT·개발")
    minor = taxonomy.create(conn, parent_id=major.id, name="서버·백엔드")

    minors = taxonomy.list_minors(conn, major.id)

    assert [m.name for m in minors] == ["서버·백엔드"]
    assert minor.parent_id == major.id


def test_같은_부모_아래_이름이_중복되면_거절된다(conn: sqlite3.Connection) -> None:
    major = taxonomy.create(conn, parent_id=None, name="IT·개발")
    taxonomy.create(conn, parent_id=major.id, name="서버·백엔드")

    with pytest.raises(taxonomy.TaxonomyError) as caught:
        taxonomy.create(conn, parent_id=major.id, name="서버·백엔드")
    assert caught.value.reason == "duplicate_name"


def test_대분류_이름_중복도_거절된다(conn: sqlite3.Connection) -> None:
    """`(parent_id, name)` UNIQUE 는 NULL 끼리는 막지 않는다 — 애플리케이션이 막는다."""
    taxonomy.create(conn, parent_id=None, name="IT·개발")

    with pytest.raises(taxonomy.TaxonomyError) as caught:
        taxonomy.create(conn, parent_id=None, name="IT·개발")
    assert caught.value.reason == "duplicate_name"


def test_없는_부모를_가리키면_거절된다(conn: sqlite3.Connection) -> None:
    with pytest.raises(taxonomy.TaxonomyError) as caught:
        taxonomy.create(conn, parent_id=999, name="서버·백엔드")
    assert caught.value.reason == "unknown_parent"


def test_소분류를_부모로_삼을_수_없다(conn: sqlite3.Connection) -> None:
    """3단계를 막는다 — 소분류 아래에 또 소분류를 달지 못한다."""
    major = taxonomy.create(conn, parent_id=None, name="IT·개발")
    minor = taxonomy.create(conn, parent_id=major.id, name="서버·백엔드")

    with pytest.raises(taxonomy.TaxonomyError) as caught:
        taxonomy.create(conn, parent_id=minor.id, name="더 깊은 것")
    assert caught.value.reason == "parent_is_minor"


def test_빈_이름은_거절된다(conn: sqlite3.Connection) -> None:
    with pytest.raises(taxonomy.TaxonomyError) as caught:
        taxonomy.create(conn, parent_id=None, name="   ")
    assert caught.value.reason == "empty_name"


def test_이름을_고치면_반영되고_중복_검사도_다시_한다(conn: sqlite3.Connection) -> None:
    major = taxonomy.create(conn, parent_id=None, name="IT·개발")
    other = taxonomy.create(conn, parent_id=None, name="AI·데이터")

    updated = taxonomy.update(conn, major.id, name="IT 개발")
    assert updated.name == "IT 개발"

    with pytest.raises(taxonomy.TaxonomyError) as caught:
        taxonomy.update(conn, other.id, name="IT 개발")
    assert caught.value.reason == "duplicate_name"


def test_끄면_꺼짐으로_바뀌고_지우는_함수는_없다(conn: sqlite3.Connection) -> None:
    major = taxonomy.create(conn, parent_id=None, name="IT·개발")

    off = taxonomy.set_enabled(conn, major.id, False)
    assert off.enabled is False

    on = taxonomy.set_enabled(conn, major.id, True)
    assert on.enabled is True

    assert not hasattr(taxonomy, "delete")


def test_켜진_것만_고르면_꺼진_것은_빠진다(conn: sqlite3.Connection) -> None:
    major = taxonomy.create(conn, parent_id=None, name="IT·개발")
    taxonomy.set_enabled(conn, major.id, False)
    taxonomy.create(conn, parent_id=None, name="AI·데이터")

    majors = taxonomy.list_majors(conn, enabled_only=True)

    assert [m.name for m in majors] == ["AI·데이터"]


def test_씨앗을_넣으면_파일과_숫자가_같다(conn: sqlite3.Connection) -> None:
    data = json.loads(SEED.read_text(encoding="utf-8"))
    expected_majors = len(data["majors"])
    expected_minors = sum(len(m["minors"]) for m in data["majors"])

    majors_added, minors_added = taxonomy.load_seed(conn, SEED)

    assert (majors_added, minors_added) == (expected_majors, expected_minors)
    assert len(taxonomy.list_majors(conn)) == expected_majors
    assert (
        sum(len(taxonomy.list_minors(conn, m.id)) for m in taxonomy.list_majors(conn))
        == expected_minors
    )


def test_표가_비어있지_않으면_씨앗을_넣지_않는다(conn: sqlite3.Connection) -> None:
    taxonomy.create(conn, parent_id=None, name="손으로 만든 대분류")

    added = taxonomy.load_seed(conn, SEED)

    assert added == (0, 0)
    assert [m.name for m in taxonomy.list_majors(conn)] == ["손으로 만든 대분류"]
