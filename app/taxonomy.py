"""직무 분류 체계. `job_taxonomy` 하나만 건드린다.

LLM이 공고를 나눌 때 고르는 목록은 코드가 아니라 이 표에 있다
(`.claude/tasks/todo/prd-job-taxonomy.md`). 읽기는 예외를 던지지 않는다 — 손으로 넣은 값
하나가 분류를 통째로 멈추게 둘 수 없다. 쓰기는 반대로 깐깐하다
(`app/notify/settings.py` 와 같은 규칙).

## 표 하나에 두 단계

`parent_id` 가 NULL 이면 대분류, 아니면 그 값이 가리키는 대분류의 소분류다
(`migrations/0024_job_taxonomy.sql`). 표를 둘로 가르면 같은 CRUD 를 두 벌 쓰게 된다.

## 대분류 이름 중복은 여기서 막는다

SQLite 의 `UNIQUE (parent_id, name)` 은 `parent_id` 가 둘 다 NULL 인 대분류 두 개가 같은
이름이어도 막지 못한다 — NULL 은 서로 다른 값으로 본다. `_check_name_unique` 가 그 자리를
메운다.

## 지우는 함수가 없다

지우면 그 값으로 분류된 공고가 목록에 없는 값을 갖고, 소비 측이 받는 값이 우리 목록 밖이
된다. 켜기·끄기만 둔다 — `set_enabled` 로 끈 값은 새 분류에서 빠지지만 이미 분류된 건은
그대로다.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from dataclasses import dataclass


class TaxonomyError(ValueError):
    """저장할 수 없다. `reason` 을 화면이 그대로 옮긴다."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class TaxonomyNode:
    """대분류 또는 소분류 한 행."""

    id: int
    parent_id: int | None
    name: str
    sort_order: int
    enabled: bool
    note: str


def _row_to_node(row: sqlite3.Row) -> TaxonomyNode:
    return TaxonomyNode(
        id=int(row["id"]),
        parent_id=None if row["parent_id"] is None else int(row["parent_id"]),
        name=str(row["name"]),
        sort_order=int(row["sort_order"]),
        enabled=bool(row["enabled"]),
        note=str(row["note"]),
    )


def list_all(conn: sqlite3.Connection) -> list[TaxonomyNode]:
    """전부, 평평하게. 계층을 그리려면 `list_majors` 와 `list_minors` 를 따로 쓴다 —
    이 함수는 대분류와 소분류를 뒤섞어 이름순으로 늘어놓을 뿐, 트리 순서를 보장하지 않는다.
    """
    rows = conn.execute(
        "SELECT id, parent_id, name, sort_order, enabled, note FROM job_taxonomy"
        " ORDER BY parent_id IS NOT NULL, parent_id, sort_order, name"
    ).fetchall()
    return [_row_to_node(row) for row in rows]


def list_majors(conn: sqlite3.Connection, *, enabled_only: bool = False) -> list[TaxonomyNode]:
    where = "WHERE parent_id IS NULL" + (" AND enabled = 1" if enabled_only else "")
    rows = conn.execute(
        f"SELECT id, parent_id, name, sort_order, enabled, note FROM job_taxonomy"
        f" {where} ORDER BY sort_order, name"
    ).fetchall()
    return [_row_to_node(row) for row in rows]


def list_minors(
    conn: sqlite3.Connection, major_id: int, *, enabled_only: bool = False
) -> list[TaxonomyNode]:
    where = "WHERE parent_id = ?" + (" AND enabled = 1" if enabled_only else "")
    rows = conn.execute(
        f"SELECT id, parent_id, name, sort_order, enabled, note FROM job_taxonomy"
        f" {where} ORDER BY sort_order, name",
        (major_id,),
    ).fetchall()
    return [_row_to_node(row) for row in rows]


def enabled_tree(conn: sqlite3.Connection) -> list[tuple[str, tuple[str, ...]]]:
    """켜진 대분류와 그 아래 켜진 소분류 이름. 분류 프롬프트가 그대로 이 모양을 쓴다.

    소분류가 하나도 켜져 있지 않은 대분류는 빈 튜플로 나온다 — 대분류 자체는 여전히 고를
    수 있어야 한다.
    """
    return [
        (major.name, tuple(minor.name for minor in list_minors(conn, major.id, enabled_only=True)))
        for major in list_majors(conn, enabled_only=True)
    ]


def read(conn: sqlite3.Connection, node_id: int) -> TaxonomyNode | None:
    row = conn.execute(
        "SELECT id, parent_id, name, sort_order, enabled, note FROM job_taxonomy WHERE id = ?",
        (node_id,),
    ).fetchone()
    return None if row is None else _row_to_node(row)


def is_empty(conn: sqlite3.Connection) -> bool:
    return int(conn.execute("SELECT count(*) FROM job_taxonomy").fetchone()[0]) == 0


def _check_name_unique(
    conn: sqlite3.Connection, parent_id: int | None, name: str, *, exclude_id: int | None = None
) -> None:
    if parent_id is None:
        rows = conn.execute(
            "SELECT id FROM job_taxonomy WHERE parent_id IS NULL AND name = ?", (name,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id FROM job_taxonomy WHERE parent_id = ? AND name = ?", (parent_id, name)
        ).fetchall()
    ids = {int(row["id"]) for row in rows}
    ids.discard(exclude_id)
    if ids:
        raise TaxonomyError("duplicate_name", f"같은 자리에 이미 있는 이름이다: {name!r}")


def _require_major(conn: sqlite3.Connection, parent_id: int) -> None:
    parent = read(conn, parent_id)
    if parent is None:
        raise TaxonomyError("unknown_parent", f"부모 id {parent_id} 가 없다")
    if parent.parent_id is not None:
        raise TaxonomyError(
            "parent_is_minor", f"부모 {parent.name!r} 는 소분류다. 소분류 아래에 또 달 수 없다"
        )


def create(
    conn: sqlite3.Connection,
    *,
    parent_id: int | None,
    name: str,
    sort_order: int = 0,
    note: str = "",
) -> TaxonomyNode:
    """대분류(`parent_id=None`) 또는 소분류를 만든다."""
    cleaned = name.strip()
    if not cleaned:
        raise TaxonomyError("empty_name", "이름이 비어 있다")
    if parent_id is not None:
        _require_major(conn, parent_id)
    _check_name_unique(conn, parent_id, cleaned)

    cursor = conn.execute(
        """
        INSERT INTO job_taxonomy (parent_id, name, sort_order, note)
        VALUES (?, ?, ?, ?)
        """,
        (parent_id, cleaned, sort_order, note.strip()),
    )
    node = read(conn, int(cursor.lastrowid or 0))
    assert node is not None
    return node


def update(
    conn: sqlite3.Connection,
    node_id: int,
    *,
    name: str | None = None,
    sort_order: int | None = None,
    note: str | None = None,
) -> TaxonomyNode:
    """이름·순서·메모를 고친다. 부모는 여기서 바꾸지 않는다 — 새로 만들어 옮긴다."""
    existing = read(conn, node_id)
    if existing is None:
        raise TaxonomyError("not_found", f"id {node_id} 가 없다")

    next_name = existing.name if name is None else name.strip()
    if not next_name:
        raise TaxonomyError("empty_name", "이름이 비어 있다")
    if next_name != existing.name:
        _check_name_unique(conn, existing.parent_id, next_name, exclude_id=node_id)

    conn.execute(
        """
        UPDATE job_taxonomy
           SET name = ?, sort_order = ?, note = ?, updated_at = datetime('now')
         WHERE id = ?
        """,
        (
            next_name,
            existing.sort_order if sort_order is None else sort_order,
            existing.note if note is None else note.strip(),
            node_id,
        ),
    )
    updated = read(conn, node_id)
    assert updated is not None
    return updated


def set_enabled(conn: sqlite3.Connection, node_id: int, enabled: bool) -> TaxonomyNode:
    existing = read(conn, node_id)
    if existing is None:
        raise TaxonomyError("not_found", f"id {node_id} 가 없다")
    conn.execute(
        "UPDATE job_taxonomy SET enabled = ?, updated_at = datetime('now') WHERE id = ?",
        (int(enabled), node_id),
    )
    updated = read(conn, node_id)
    assert updated is not None
    return updated


def load_seed(conn: sqlite3.Connection, path: pathlib.Path) -> tuple[int, int]:
    """씨앗 파일을 넣는다. 표가 완전히 비어 있을 때만 동작한다.

    이미 운영자가 고친 표 위에 씨앗을 다시 부으면 손으로 넣은 값이 씨앗과 뒤섞인다
    (`.claude/tasks/todo/prd-job-taxonomy.md` 3절). 비어 있지 않으면 아무 일도 하지 않고
    `(0, 0)` 을 돌려준다.
    """
    if not is_empty(conn):
        return (0, 0)

    data = json.loads(path.read_text(encoding="utf-8"))
    majors_added = 0
    minors_added = 0
    for order, major in enumerate(data["majors"]):
        major_node = create(conn, parent_id=None, name=major["name"], sort_order=order)
        majors_added += 1
        for minor_order, minor_name in enumerate(major["minors"]):
            create(conn, parent_id=major_node.id, name=minor_name, sort_order=minor_order)
            minors_added += 1
    return (majors_added, minors_added)


@dataclass(frozen=True)
class MinorEdit:
    """수정 화면이 보낸 소분류 한 줄. `id` 가 없으면 새로 더한 줄이다. 순서는 목록 순서다."""

    id: int | None
    name: str
    enabled: bool


def save_major(
    conn: sqlite3.Connection,
    major_id: int,
    *,
    name: str,
    enabled: bool,
    minors: list[MinorEdit],
) -> list[tuple[str, str]]:
    """대분류 하나와 그 아래 소분류를 화면에 보인 대로 한 번에 저장한다 (2026-09-17 결정).

    수정 화면은 대분류 이름·켜짐과 소분류 줄들을 한꺼번에 보낸다. 순서는 보낸 순서 그대로다.
    한 줄이라도 틀리면(빈 이름, 겹친 이름) 아무것도 저장하지 않는다 — 절반만 바뀐 분류표는
    운영자가 손으로 되돌릴 수 없다. 보내지 않은 소분류도 지우지 않는다(위 "지우는 함수가 없다").

    **이름을 바꾸면 그 이름으로 이미 분류된 공고의 값도 같이 바꾼다.** 예전에는 "어긋난다" 고
    알리기만 해서, 이름 하나 고칠 때마다 공고들이 목록 밖의 값을 갖게 됐다. 분류 결과와 정규화
    결과 둘 다 이름을 저장하므로 둘 다 바꾼다. 바꾼 (옛 이름, 새 이름) 을 돌려준다.
    """
    major = read(conn, major_id)
    if major is None or major.parent_id is not None:
        raise TaxonomyError("not_found", f"대분류 id {major_id} 가 없다")
    major_name = name.strip()
    if not major_name:
        raise TaxonomyError("empty_name", "대분류 이름이 비어 있다")
    cleaned = [MinorEdit(minor.id, minor.name.strip(), minor.enabled) for minor in minors]
    names = [minor.name for minor in cleaned]
    if any(not item for item in names):
        raise TaxonomyError("empty_name", "이름이 빈 소분류가 있다")
    duplicates = sorted({item for item in names if names.count(item) > 1})
    if duplicates:
        raise TaxonomyError("duplicate_name", f"같은 이름이 두 번 있다: {', '.join(duplicates)}")
    existing = {minor.id: minor for minor in list_minors(conn, major_id)}
    unknown = [minor.id for minor in cleaned if minor.id is not None and minor.id not in existing]
    if unknown:
        raise TaxonomyError("not_found", f"이 대분류에 없는 소분류다: {unknown}")
    sent = {minor.id for minor in cleaned}
    kept_names = {minor.name for minor_id, minor in existing.items() if minor_id not in sent}
    clash = sorted(set(names) & kept_names)
    if clash:
        raise TaxonomyError("duplicate_name", f"같은 자리에 이미 있는 이름이다: {', '.join(clash)}")
    if major_name != major.name:
        _check_name_unique(conn, None, major_name, exclude_id=major_id)

    renamed: list[tuple[str, str]] = []
    conn.execute("SAVEPOINT save_major")
    try:
        if major_name != major.name:
            _rename_jobs(conn, "job_field", major.name, major_name)
            renamed.append((major.name, major_name))
        conn.execute(
            "UPDATE job_taxonomy SET name = ?, enabled = ?, updated_at = datetime('now')"
            " WHERE id = ?",
            (major_name, int(enabled), major_id),
        )
        # 이름을 서로 맞바꾸는 저장도 있다. UNIQUE 에 걸리지 않게 먼저 임시 이름으로 비킨다
        for minor in cleaned:
            if minor.id is not None and existing[minor.id].name != minor.name:
                conn.execute(
                    "UPDATE job_taxonomy SET name = ? WHERE id = ?",
                    (f"\x00{minor.id}", minor.id),
                )
        for order, minor in enumerate(cleaned):
            if minor.id is None:
                conn.execute(
                    "INSERT INTO job_taxonomy (parent_id, name, sort_order, enabled)"
                    " VALUES (?, ?, ?, ?)",
                    (major_id, minor.name, order, int(minor.enabled)),
                )
                continue
            before = existing[minor.id]
            if before.name != minor.name:
                _rename_jobs(conn, "job_role", before.name, minor.name, job_field=major_name)
                renamed.append((before.name, minor.name))
            conn.execute(
                "UPDATE job_taxonomy SET name = ?, sort_order = ?, enabled = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (minor.name, order, int(minor.enabled), minor.id),
            )
        conn.execute("RELEASE save_major")
    except Exception:
        conn.execute("ROLLBACK TO save_major")
        conn.execute("RELEASE save_major")
        raise
    return renamed


def add_major(conn: sqlite3.Connection, name: str) -> TaxonomyNode:
    """대분류를 맨 뒤에 더한다."""
    row = conn.execute(
        "SELECT coalesce(max(sort_order), -1) + 1 FROM job_taxonomy WHERE parent_id IS NULL"
    ).fetchone()
    return create(conn, parent_id=None, name=name, sort_order=int(row[0]))


def _rename_jobs(
    conn: sqlite3.Connection, column: str, old: str, new: str, *, job_field: str | None = None
) -> None:
    """이미 분류된 공고의 직군·직무 이름을 바꾼다. 직무는 그 직군 안의 것만 바꾼다.

    직군 이름을 먼저 바꾸므로 직무를 바꿀 때 보는 직군은 새 이름이다.
    """
    where = f"{column} = ?" + (" AND job_field = ?" if job_field is not None else "")
    params: tuple[str, ...] = (new, old) + ((job_field,) if job_field is not None else ())
    for table in ("job_classifications", "normalized_jobs"):
        conn.execute(f"UPDATE {table} SET {column} = ? WHERE {where}", params)
