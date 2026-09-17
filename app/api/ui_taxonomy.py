"""직무 분류 화면의 조각 라우트.

2026-09-17 결정(LC-3344). 왼쪽에 대분류, 오른쪽에 고른 대분류의 소분류를 칩으로 보인다 — 채용
사이트의 직무 고르기 화면과 같은 모양이라 단계가 눈에 보이고, "대분류/소분류" 구분 칸이 필요 없다.
평소에는 읽기만 하고, `수정` 을 누르면 이름·순서·켜짐을 고친 뒤 `저장` 한 번으로 저장한다.
줄마다 저장 단추가 있던 예전 표는 무엇이 저장됐는지 알기 어려웠다. 메모 칸은 AI 에게 가지 않는
값이라 뺐다.

체계 CRUD 는 `app/taxonomy.py` 를 그대로 부른다.

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


def _job_counts(conn: sqlite3.Connection, column: str) -> dict[str, int]:
    """`column`(`job_field` 또는 `job_role`) 값별 공고 수. 호출부가 고정된 두 이름만 넘긴다."""
    rows = conn.execute(
        f"SELECT {column} AS name, COUNT(*) AS n FROM normalized_jobs"
        f" WHERE {column} IS NOT NULL GROUP BY {column}"
    ).fetchall()
    return {str(row["name"]): int(row["n"]) for row in rows}


def _view(
    request: Request,
    conn: sqlite3.Connection,
    *,
    major_id: int | None = None,
    edit: bool = False,
    message: str = "",
    error: str = "",
    draft: dict[str, object] | None = None,
) -> HTMLResponse:
    """왼쪽 대분류 목록과 오른쪽에 고른 대분류의 소분류. 모든 동작이 이 조각으로 돌아온다.

    `draft` 는 저장이 거절됐을 때 사용자가 넣은 값이다. 거절한 뒤 저장된 값으로 되돌리면
    고친 것을 처음부터 다시 해야 한다.
    """
    majors = taxonomy.list_majors(conn)
    selected = next((major for major in majors if major.id == major_id), None)
    if selected is None and majors:
        selected = majors[0]
    minors = taxonomy.list_minors(conn, selected.id) if selected else []
    enabled_counts = {
        major.id: len(taxonomy.list_minors(conn, major.id, enabled_only=True)) for major in majors
    }
    return render(
        request,
        "fragments/taxonomy_tree.html",
        majors=majors,
        selected=selected,
        minors=minors,
        enabled_counts=enabled_counts,
        field_count=_job_counts(conn, "job_field").get(selected.name, 0) if selected else 0,
        role_counts=_job_counts(conn, "job_role"),
        edit=edit and selected is not None,
        is_empty=taxonomy.is_empty(conn),
        message=message,
        error=error,
        draft=draft,
    )


@router.get("/ui/taxonomy", response_class=HTMLResponse)
def taxonomy_tree_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    major: int | None = None,
    edit: bool = False,
) -> HTMLResponse:
    return _view(request, conn, major_id=major, edit=edit)


@router.post("/ui/taxonomy/majors", response_class=HTMLResponse)
def add_major_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    name: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """대분류를 맨 뒤에 더하고, 곧바로 소분류를 넣을 수 있게 수정 화면으로 연다."""
    try:
        created = taxonomy.add_major(conn, name)
    except taxonomy.TaxonomyError as exc:
        return _view(request, conn, error=str(exc))
    return _view(request, conn, major_id=created.id, edit=True)


@router.put("/ui/taxonomy/{major_id}", response_class=HTMLResponse)
def save_major_fragment(
    request: Request,
    major_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    name: Annotated[str, Form()] = "",
    enabled: Annotated[str, Form()] = "",
    minor_id: Annotated[list[str] | None, Form()] = None,
    minor_name: Annotated[list[str] | None, Form()] = None,
    minor_on: Annotated[list[str] | None, Form()] = None,
) -> HTMLResponse:
    """수정 화면에 보인 대로 대분류와 그 소분류를 저장한다. 순서는 화면의 줄 순서다."""
    ids, names, ons = minor_id or [], minor_name or [], minor_on or []
    minors = [
        taxonomy.MinorEdit(
            int(ids[index]) if index < len(ids) and ids[index].isdigit() else None,
            minor,
            index < len(ons) and ons[index] == "1",
        )
        for index, minor in enumerate(names)
    ]
    try:
        renamed = taxonomy.save_major(
            conn, major_id, name=name, enabled=enabled == "1", minors=minors
        )
    except taxonomy.TaxonomyError as exc:
        draft = {"name": name, "enabled": enabled == "1", "minors": minors}
        return _view(request, conn, major_id=major_id, edit=True, error=str(exc), draft=draft)
    message = "저장했습니다"
    if renamed:
        changes = ", ".join(f"{old} → {new}" for old, new in renamed)
        message += f". 이미 분류된 공고의 이름도 바꿨습니다 ({changes})"
    return _view(request, conn, major_id=major_id, message=message)


@router.post("/ui/taxonomy/seed", response_class=HTMLResponse)
def seed_taxonomy_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> HTMLResponse:
    """씨앗 파일을 한 번에 넣는다. 표가 비어 있지 않으면 `load_seed` 가 아무 일도 하지
    않는다 — 화면에는 표가 비어 있을 때만 이 단추 자체가 없다(`taxonomy_tree.html`)."""
    majors_added, minors_added = taxonomy.load_seed(conn, SEED_PATH)
    if majors_added == 0 and minors_added == 0:
        return _view(
            request, conn, error="표가 이미 비어 있지 않아 기본 분류를 다시 불러오지 않았다"
        )
    return _view(
        request,
        conn,
        message=f"기본 분류를 불러왔다: 대분류 {majors_added}개, 소분류 {minors_added}개",
    )
