"""0039 는 분류 워크플로우가 없을 때만 "수집 직후 AI 분류" 를 켜진 채로 넣는다.

| 확인 | 깨지면 |
|---|---|
| 빈 DB 에는 켜진 수집 직후 분류가 생긴다 | 수집해도 분류 버튼을 매번 눌러야 한다 |
| 분류 워크플로우를 이미 만든 곳은 건드리지 않는다 | 운영자가 끈 분류가 되살아난다 |
| 되돌리면 넣은 행만 지운다 | 운영자가 만든 워크플로우가 사라진다 |

`tests/conftest.py` 가 테스트 DB 에서 이 행을 지우므로 원래 `migrate_up` 을 부른다.
"""

from __future__ import annotations

import pathlib
import sqlite3

from app import db


def _up(conn: sqlite3.Connection) -> None:
    db.migrate_up.original(conn)  # type: ignore[attr-defined]


def _rows(conn: sqlite3.Connection) -> list[tuple[str, str, str, int]]:
    return [
        (row["name"], row["status"], row["trigger_kind"], row["batch_limit"])
        for row in conn.execute(
            "SELECT name, status, trigger_kind, batch_limit FROM side_workflows ORDER BY id"
        )
    ]


def test_빈_DB_에는_켜진_수집_직후_분류가_생긴다(tmp_path: pathlib.Path) -> None:
    conn = db.connect(tmp_path / "jobs.db")
    try:
        _up(conn)
        assert _rows(conn) == [("수집 직후 AI 분류", "active", "after_crawl", 200)]
    finally:
        conn.close()


def test_분류_워크플로우가_있으면_건드리지_않고_되돌리면_넣은_행만_지운다(
    tmp_path: pathlib.Path,
) -> None:
    conn = db.connect(tmp_path / "jobs.db")
    try:
        _up(conn)
        db.migrate_down(conn)
        conn.execute("DELETE FROM side_workflows")
        conn.execute(
            "INSERT INTO side_workflows (kind, name, target_scope) VALUES"
            " ('classify', '내가 만든 분류', 'unclassified')"
        )
        _up(conn)
        assert _rows(conn) == [("내가 만든 분류", "paused", "manual", 50)]

        db.migrate_down(conn)
        assert [row[0] for row in _rows(conn)] == ["내가 만든 분류"]
    finally:
        conn.close()
