"""기동 시 빈 분류표 채우기.

직무 분류표와 산업 분류표가 비면 분류가 직군·직무·산업을 묻지 않고 빈 채로 저장한다. 에러가
없어 새로 띄운 서버에서 세 칸이 조용히 빈다 (2026-09-18 확인). 그래서 실제 `lifespan` 을 지나
기동 한 번으로 두 표가 차는지 본다.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from app import db, industries, taxonomy
from app.config import get_settings
from app.main import app, lifespan


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "jobs.db"
    conn = db.connect(path)
    db.migrate_up(conn)
    conn.close()

    previous = os.environ.get("DATABASE_PATH")
    os.environ["DATABASE_PATH"] = str(path)
    get_settings.cache_clear()
    try:
        yield path
    finally:
        if previous is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = previous
        get_settings.cache_clear()


async def test_startup_seeds_empty_tables(database: Path) -> None:
    async with lifespan(app):
        pass

    conn = db.connect(database)
    try:
        assert taxonomy.enabled_tree(conn)
        assert industries.enabled_names(conn)
    finally:
        conn.close()


async def test_startup_leaves_edited_tables_alone(database: Path) -> None:
    """운영자가 고친 표는 한 줄이라도 있으면 건드리지 않는다."""
    conn = db.connect(database)
    taxonomy.create(conn, parent_id=None, name="직접 넣은 직군", sort_order=0)
    industries.create(conn, name="직접 넣은 산업", sort_order=0)
    conn.close()

    async with lifespan(app):
        pass

    conn = db.connect(database)
    try:
        assert [major.name for major in taxonomy.list_majors(conn)] == ["직접 넣은 직군"]
        assert industries.enabled_names(conn) == ("직접 넣은 산업",)
    finally:
        conn.close()
