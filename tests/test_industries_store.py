"""산업 분류표 저장소 (2026-09-14 결정).

실사이트에 나가지 않는다. `app/industries.py` 를 실제 마이그레이션을 올린 DB 에 돌린다.

| 확인 | 깨지면 |
|---|---|
| 만들고 고치고 끄면 켜진 산업만 순서대로 분류에 간다 | 끈 산업이 계속 골라진다 |
| 이름이 비거나 겹치면 사유를 댄다 | UNIQUE 제약이 화면에 500 을 낸다 |
| 없는 산업은 고칠 수 없다 | 조용히 아무 일도 하지 않는다 |
| 씨앗은 빈 표에만 잡코리아 산업 11개를 넣는다 | 운영자가 고친 표에 씨앗이 섞인다 |
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from app import db, industries

SEED = pathlib.Path(__file__).parent.parent / "seeds" / "industries-jobkorea-20260914.json"


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    try:
        yield connection
    finally:
        connection.close()


def test_만들고_고치고_끄면_켜진_산업만_순서대로_나온다(conn: sqlite3.Connection) -> None:
    finance = industries.create(conn, name=" 금융·은행업 ", sort_order=1)
    it = industries.create(conn, name="IT·정보통신업", sort_order=0, note="메모")

    assert finance.name == "금융·은행업"
    assert industries.enabled_names(conn) == ("IT·정보통신업", "금융·은행업")

    renamed = industries.update(conn, it.id, name="IT·통신업", note="")
    industries.set_enabled(conn, finance.id, False)

    assert (renamed.name, renamed.sort_order, renamed.note) == ("IT·통신업", 0, "")
    assert industries.enabled_names(conn) == ("IT·통신업",)
    assert [item.name for item in industries.list_all(conn)] == ["IT·통신업", "금융·은행업"]


@pytest.mark.parametrize("name", ["", "   "])
def test_이름이_비면_거절한다(conn: sqlite3.Connection, name: str) -> None:
    with pytest.raises(industries.IndustryError) as caught:
        industries.create(conn, name=name)

    assert caught.value.reason == "empty_name"


def test_이름이_겹치면_거절한다(conn: sqlite3.Connection) -> None:
    industries.create(conn, name="건설업")
    other = industries.create(conn, name="교육업")

    with pytest.raises(industries.IndustryError) as created:
        industries.create(conn, name="건설업")
    with pytest.raises(industries.IndustryError) as updated:
        industries.update(conn, other.id, name="건설업")

    assert created.value.reason == updated.value.reason == "duplicate_name"
    # 제 이름 그대로 저장하는 것은 겹친 것이 아니다
    assert industries.update(conn, other.id, name="교육업", sort_order=3).sort_order == 3


def test_없는_산업은_고칠_수_없다(conn: sqlite3.Connection) -> None:
    with pytest.raises(industries.IndustryError) as updated:
        industries.update(conn, 99, name="건설업")
    with pytest.raises(industries.IndustryError) as toggled:
        industries.set_enabled(conn, 99, True)

    assert updated.value.reason == toggled.value.reason == "not_found"


def test_씨앗은_빈_표에만_잡코리아_산업_열한_개를_넣는다(conn: sqlite3.Connection) -> None:
    assert industries.load_seed(conn, SEED) == 11
    assert industries.enabled_names(conn) == (
        "서비스업",
        "금융·은행업",
        "IT·정보통신업",
        "판매·유통업",
        "제조·생산·화학업",
        "교육업",
        "건설업",
        "의료·제약업",
        "미디어·광고업",
        "문화·예술·디자인업",
        "기관·협회",
    )

    assert industries.load_seed(conn, SEED) == 0
    assert len(industries.list_all(conn)) == 11
