"""직무 분류 화면의 조각 라우트.

체계 CRUD 는 `app/taxonomy.py` 를 그대로 부른다. 이 파일이 더하는 것은 화면이 필요로 하는
두 가지뿐이다 — 대분류 아래 소분류를 묶어 트리로 그리는 것, 그리고 그 이름으로 이미
분류된 공고 수를 얹는 것.

## 공고 수는 여기서 센다

`app/taxonomy.py` 는 `job_taxonomy` 하나만 읽는다. 공고 수는 `normalized_jobs` 를 함께
읽어야 하고, 그 셈이 저장소 모듈에 들어가면 표 한 행을 고치는 일과 공고를 세는 일이 한
자리에 섞인다(`app/api/ui_companies.py` 와 같은 이유).

이름으로 잇는다. `job_taxonomy` 는 아이디를 갖지만 `normalized_jobs.job_field`/`job_role`
는 이름을 저장하므로(PRD 1절 — 재정규화로 다시 만들어지는 파생 표라 id 를 넣으면 소비 측이
표를 한 벌 더 갖게 된다), 세는 것도 이름으로 잇는다.
"""

from __future__ import annotations

import pathlib
import sqlite3
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app import taxonomy
from app.api.settings import get_connection
from app.api.ui import render

router = APIRouter(tags=["ui"], include_in_schema=False)

# 씨앗 파일 하나. `app/taxonomy.py::load_seed` 가 표가 완전히 비어 있을 때만 넣는다 —
# 이미 고친 표 위에 다시 부어 손으로 넣은 값과 뒤섞이는 일은 저장소 쪽에서 막는다
SEED_PATH = pathlib.Path(__file__).resolve().parent.parent.parent / (
    "seeds/job-taxonomy-zighang-20260828.json"
)


@dataclass(frozen=True)
class TaxonomyRow:
    """화면이 그리는 한 줄. 저장된 노드에 그 이름을 가진 공고 수를 얹은 것이다."""

    node: taxonomy.TaxonomyNode
    job_count: int


def _job_counts(conn: sqlite3.Connection, column: str) -> dict[str, int]:
    """`column`(`job_field` 또는 `job_role`) 값별 공고 수. 호출부가 고정된 두 이름만 넘긴다."""
    rows = conn.execute(
        f"SELECT {column} AS name, COUNT(*) AS n FROM normalized_jobs"
        f" WHERE {column} IS NOT NULL GROUP BY {column}"
    ).fetchall()
    return {str(row["name"]): int(row["n"]) for row in rows}


def _name_count(conn: sqlite3.Connection, column: str, name: str) -> int:
    """이름 하나의 공고 수. 이름을 고치기 전에 그 값을 화면에 보여줄 때 쓴다."""
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM normalized_jobs WHERE {column} = ?", (name,)
    ).fetchone()
    return int(row["n"]) if row is not None else 0


def build_tree(conn: sqlite3.Connection) -> list[tuple[TaxonomyRow, list[TaxonomyRow]]]:
    """대분류와 그 아래 소분류를 묶어, 각자의 공고 수와 함께 늘어놓는다."""
    major_counts = _job_counts(conn, "job_field")
    minor_counts = _job_counts(conn, "job_role")
    tree: list[tuple[TaxonomyRow, list[TaxonomyRow]]] = []
    for major in taxonomy.list_majors(conn):
        major_row = TaxonomyRow(major, major_counts.get(major.name, 0))
        minor_rows = [
            TaxonomyRow(minor, minor_counts.get(minor.name, 0))
            for minor in taxonomy.list_minors(conn, major.id)
        ]
        tree.append((major_row, minor_rows))
    return tree


def _tree(
    request: Request,
    conn: sqlite3.Connection,
    *,
    message: str = "",
    error: dict[str, str] | None = None,
) -> HTMLResponse:
    """트리 조각 하나. 더하기·고치기·켜기끄기·씨앗 넣기가 모두 이 조각으로 돌아온다."""
    return render(
        request,
        "fragments/taxonomy_tree.html",
        tree=build_tree(conn),
        majors=taxonomy.list_majors(conn),
        is_empty=taxonomy.is_empty(conn),
        message=message,
        error=error,
    )


@router.get("/ui/taxonomy", response_class=HTMLResponse)
def taxonomy_tree_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> HTMLResponse:
    return _tree(request, conn)


@router.post("/ui/taxonomy", response_class=HTMLResponse)
def create_node_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    name: Annotated[str, Form()],
    parent_id: Annotated[str, Form()] = "",
    sort_order: Annotated[int, Form()] = 0,
    note: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """대분류(`parent_id` 없음) 또는 소분류를 더한다."""
    parsed_parent = int(parent_id) if parent_id.strip() else None
    try:
        created = taxonomy.create(
            conn, parent_id=parsed_parent, name=name, sort_order=sort_order, note=note
        )
    except taxonomy.TaxonomyError as exc:
        return _tree(request, conn, error={"reason": exc.reason, "message": str(exc)})

    kind = "대분류" if created.parent_id is None else "소분류"
    return _tree(request, conn, message=f"{kind} '{created.name}' 를 더했다")


@router.put("/ui/taxonomy/{node_id}", response_class=HTMLResponse)
def update_node_fragment(
    request: Request,
    node_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    name: Annotated[str, Form()],
    sort_order: Annotated[int, Form()] = 0,
    note: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """이름·순서·메모를 저장한다. 이름이 바뀌면 옛 이름으로 이미 분류된 공고 수를 함께
    알린다 — 저장 순간 그 건들의 값과 목록의 이름이 어긋난다(PRD 3절).
    """
    existing = taxonomy.read(conn, node_id)
    if existing is None:
        return _tree(
            request, conn, error={"reason": "not_found", "message": f"id {node_id} 가 없다"}
        )

    column = "job_field" if existing.parent_id is None else "job_role"
    old_name = existing.name
    old_count = _name_count(conn, column, old_name)

    try:
        updated = taxonomy.update(conn, node_id, name=name, sort_order=sort_order, note=note)
    except taxonomy.TaxonomyError as exc:
        return _tree(request, conn, error={"reason": exc.reason, "message": str(exc)})

    if updated.name != old_name and old_count > 0:
        message = (
            f"'{old_name}' 를 '{updated.name}' 로 고쳤다. "
            f"'{old_name}' 으로 이미 분류된 공고 {old_count}건은 새 이름과 어긋난다"
        )
    else:
        message = f"'{updated.name}' 를 저장했다"
    return _tree(request, conn, message=message)


@router.post("/ui/taxonomy/{node_id}/toggle", response_class=HTMLResponse)
def toggle_node_fragment(
    request: Request,
    node_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> HTMLResponse:
    """켜짐·꺼짐만 뒤집는다. 지우는 라우트는 없다 — 끈 값으로 이미 분류된 공고는 그대로
    남고, 그 값은 새 분류에서만 빠진다."""
    existing = taxonomy.read(conn, node_id)
    if existing is None:
        return _tree(
            request, conn, error={"reason": "not_found", "message": f"id {node_id} 가 없다"}
        )

    updated = taxonomy.set_enabled(conn, node_id, not existing.enabled)
    state = "켰다" if updated.enabled else "껐다"
    return _tree(request, conn, message=f"'{updated.name}' 를 {state}")


@router.post("/ui/taxonomy/seed", response_class=HTMLResponse)
def seed_taxonomy_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> HTMLResponse:
    """씨앗 파일을 한 번에 넣는다. 표가 비어 있지 않으면 `load_seed` 가 아무 일도 하지
    않는다 — 화면에는 표가 비어 있을 때만 이 단추 자체가 없다(`taxonomy_tree.html`)."""
    majors_added, minors_added = taxonomy.load_seed(conn, SEED_PATH)
    if majors_added == 0 and minors_added == 0:
        return _tree(
            request,
            conn,
            error={
                "reason": "not_empty",
                "message": "표가 이미 비어 있지 않아 기본 분류를 다시 불러오지 않았다",
            },
        )
    return _tree(
        request,
        conn,
        message=f"기본 분류를 불러왔다: 대분류 {majors_added}개, 소분류 {minors_added}개",
    )
