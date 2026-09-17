"""산업 분류 화면의 조각 라우트 (2026-09-14 결정).

2026-09-17(LC-3344) 직무 분류 화면과 같이 바꿨다. 평소에는 칩으로 읽기만 하고, `수정` 을 누르면
줄마다 이름·순서·켜짐을 고쳐 `저장` 한 번으로 저장한다. 메모 칸은 AI 에게 가지 않는 값이라 뺐다.

표 CRUD 는 `app/industries.py` 를 그대로 부른다. 이 파일이 더하는 것은 그 이름으로 이미 분류된 공고
수를 얹는 것뿐이다 — 직무 분류 화면(`app/api/ui_taxonomy.py`)과 같은 모양이다. 공고 수를 이 파일이
세는 이유도 같다. 저장소 모듈이 `normalized_jobs` 까지 읽으면 표 한 행을 고치는 일과 공고를 세는
일이 한 자리에 섞인다.
"""

from __future__ import annotations

import pathlib
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app import industries
from app.api.settings import get_connection
from app.api.ui import render

router = APIRouter(tags=["ui"], include_in_schema=False)

# 씨앗 파일 하나. `app/industries.py::load_seed` 가 표가 완전히 비어 있을 때만 넣는다
SEED_PATH = pathlib.Path(__file__).resolve().parent.parent.parent / (
    "seeds/industries-jobkorea-20260914.json"
)


def _job_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT industry AS name, COUNT(*) AS n FROM normalized_jobs"
        " WHERE industry IS NOT NULL GROUP BY industry"
    ).fetchall()
    return {str(row["name"]): int(row["n"]) for row in rows}


def _list(
    request: Request,
    conn: sqlite3.Connection,
    *,
    edit: bool = False,
    message: str = "",
    error: str = "",
    draft: list[industries.IndustryEdit] | None = None,
) -> HTMLResponse:
    """산업 칩 목록, 또는 `수정` 을 누른 뒤의 줄 목록. 모든 동작이 이 조각으로 돌아온다."""
    return render(
        request,
        "fragments/industry_list.html",
        items=industries.list_all(conn),
        counts=_job_counts(conn),
        edit=edit,
        draft=draft,
        is_empty=industries.is_empty(conn),
        message=message,
        error=error,
    )


@router.get("/ui/industries", response_class=HTMLResponse)
def industry_list_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    edit: bool = False,
) -> HTMLResponse:
    return _list(request, conn, edit=edit)


@router.put("/ui/industries", response_class=HTMLResponse)
def save_industries_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    industry_id: Annotated[list[str] | None, Form()] = None,
    industry_name: Annotated[list[str] | None, Form()] = None,
    industry_on: Annotated[list[str] | None, Form()] = None,
) -> HTMLResponse:
    """수정 화면에 보인 대로 저장한다. 순서는 화면의 줄 순서다."""
    ids, names, ons = industry_id or [], industry_name or [], industry_on or []
    rows = [
        industries.IndustryEdit(
            int(ids[index]) if index < len(ids) and ids[index].isdigit() else None,
            name,
            index < len(ons) and ons[index] == "1",
        )
        for index, name in enumerate(names)
    ]
    try:
        renamed = industries.save_all(conn, rows)
    except industries.IndustryError as exc:
        return _list(request, conn, edit=True, error=str(exc), draft=rows)
    message = "저장했습니다"
    if renamed:
        changes = ", ".join(f"{old} → {new}" for old, new in renamed)
        message += f". 이미 분류된 공고의 이름도 바꿨습니다 ({changes})"
    return _list(request, conn, message=message)


@router.post("/ui/industries/seed", response_class=HTMLResponse)
def seed_industries_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> HTMLResponse:
    added = industries.load_seed(conn, SEED_PATH)
    if added == 0:
        return _list(
            request, conn, error="표가 이미 비어 있지 않아 기본 산업을 다시 불러오지 않았다"
        )
    return _list(request, conn, message=f"기본 산업 {added}개를 불러왔다")
