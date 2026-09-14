"""산업 분류표. `industries` 하나만 건드린다 (2026-09-14 결정).

오공고(Spring) `Job.industry` 에 보낼 산업을 분류가 공고마다 이 표에서 고른다
(`migrations/0034_industries.sql`). 직무 분류(`app/taxonomy.py`)와 같은 규칙을 따른다 — 읽기는
예외를 던지지 않고, 쓰기는 깐깐하다. 다른 점은 한 단계뿐이라는 것이다.

## 공고마다 고른다

같은 회사의 공고는 대개 산업이 같지만 회사 단위로 두지 않는다. 오공고가 공고마다 산업을 받고, 틀린
것은 검수 화면에서 그 공고만 고친다 (2026-09-14 결정).

## 지우는 함수가 없다

지우면 그 산업으로 분류된 공고가 목록에 없는 값을 갖는다. 켜기·끄기만 둔다 — 끈 산업은 새 분류에서
빠지고, 이미 분류된 공고는 그대로다.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from dataclasses import dataclass


class IndustryError(ValueError):
    """저장할 수 없다. `reason` 을 화면이 그대로 옮긴다."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Industry:
    """산업 한 행."""

    id: int
    name: str
    sort_order: int
    enabled: bool
    note: str


_COLUMNS = "id, name, sort_order, enabled, note"


def _row_to_industry(row: sqlite3.Row) -> Industry:
    return Industry(
        id=int(row["id"]),
        name=str(row["name"]),
        sort_order=int(row["sort_order"]),
        enabled=bool(row["enabled"]),
        note=str(row["note"]),
    )


def list_all(conn: sqlite3.Connection) -> list[Industry]:
    """전부. 화면과 프롬프트의 순서다."""
    rows = conn.execute(f"SELECT {_COLUMNS} FROM industries ORDER BY sort_order, name").fetchall()
    return [_row_to_industry(row) for row in rows]


def enabled_names(conn: sqlite3.Connection) -> tuple[str, ...]:
    """켜진 산업 이름. 분류 프롬프트와 응답 스키마가 이 목록을 쓴다."""
    rows = conn.execute(
        "SELECT name FROM industries WHERE enabled = 1 ORDER BY sort_order, name"
    ).fetchall()
    return tuple(str(row["name"]) for row in rows)


def read(conn: sqlite3.Connection, industry_id: int) -> Industry | None:
    row = conn.execute(f"SELECT {_COLUMNS} FROM industries WHERE id = ?", (industry_id,)).fetchone()
    return None if row is None else _row_to_industry(row)


def is_empty(conn: sqlite3.Connection) -> bool:
    return int(conn.execute("SELECT count(*) FROM industries").fetchone()[0]) == 0


def _check_name_unique(
    conn: sqlite3.Connection, name: str, *, exclude_id: int | None = None
) -> None:
    """UNIQUE 제약이 막기 전에 사유를 붙여 거절한다. 제약만 두면 화면이 500 을 본다."""
    rows = conn.execute("SELECT id FROM industries WHERE name = ?", (name,)).fetchall()
    ids = {int(row["id"]) for row in rows}
    ids.discard(exclude_id)
    if ids:
        raise IndustryError("duplicate_name", f"이미 있는 산업이다: {name!r}")


def create(conn: sqlite3.Connection, *, name: str, sort_order: int = 0, note: str = "") -> Industry:
    cleaned = name.strip()
    if not cleaned:
        raise IndustryError("empty_name", "이름이 비어 있다")
    _check_name_unique(conn, cleaned)
    cursor = conn.execute(
        "INSERT INTO industries (name, sort_order, note) VALUES (?, ?, ?)",
        (cleaned, sort_order, note.strip()),
    )
    created = read(conn, int(cursor.lastrowid or 0))
    assert created is not None
    return created


def update(
    conn: sqlite3.Connection,
    industry_id: int,
    *,
    name: str | None = None,
    sort_order: int | None = None,
    note: str | None = None,
) -> Industry:
    """이름·순서·메모를 고친다."""
    existing = read(conn, industry_id)
    if existing is None:
        raise IndustryError("not_found", f"id {industry_id} 가 없다")
    next_name = existing.name if name is None else name.strip()
    if not next_name:
        raise IndustryError("empty_name", "이름이 비어 있다")
    if next_name != existing.name:
        _check_name_unique(conn, next_name, exclude_id=industry_id)
    conn.execute(
        """
        UPDATE industries
           SET name = ?, sort_order = ?, note = ?, updated_at = datetime('now')
         WHERE id = ?
        """,
        (
            next_name,
            existing.sort_order if sort_order is None else sort_order,
            existing.note if note is None else note.strip(),
            industry_id,
        ),
    )
    updated = read(conn, industry_id)
    assert updated is not None
    return updated


def set_enabled(conn: sqlite3.Connection, industry_id: int, enabled: bool) -> Industry:
    existing = read(conn, industry_id)
    if existing is None:
        raise IndustryError("not_found", f"id {industry_id} 가 없다")
    conn.execute(
        "UPDATE industries SET enabled = ?, updated_at = datetime('now') WHERE id = ?",
        (int(enabled), industry_id),
    )
    updated = read(conn, industry_id)
    assert updated is not None
    return updated


def load_seed(conn: sqlite3.Connection, path: pathlib.Path) -> int:
    """씨앗 파일을 넣는다. 표가 완전히 비어 있을 때만 동작하고 넣은 개수를 돌려준다.

    운영자가 고친 표 위에 씨앗을 다시 부으면 손으로 넣은 값과 뒤섞인다. 비어 있지 않으면 아무 일도
    하지 않고 0 이다.
    """
    if not is_empty(conn):
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    for order, name in enumerate(data["industries"]):
        create(conn, name=name, sort_order=order)
    return len(data["industries"])
