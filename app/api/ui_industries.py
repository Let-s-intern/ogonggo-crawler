"""산업 분류 화면의 조각 라우트 (2026-09-14 결정).

표 CRUD 는 `app/industries.py` 를 그대로 부른다. 이 파일이 더하는 것은 그 이름으로 이미 분류된 공고
수를 얹는 것뿐이다 — 직무 분류 화면(`app/api/ui_taxonomy.py`)과 같은 모양이다. 공고 수를 이 파일이
세는 이유도 같다. 저장소 모듈이 `normalized_jobs` 까지 읽으면 표 한 행을 고치는 일과 공고를 세는
일이 한 자리에 섞인다.
"""

from __future__ import annotations

import pathlib
import sqlite3
from dataclasses import dataclass
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


@dataclass(frozen=True)
class IndustryRow:
    """화면이 그리는 한 줄. 저장된 산업에 그 이름으로 분류된 공고 수를 얹은 것이다."""

    industry: industries.Industry
    job_count: int


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
    message: str = "",
    error: dict[str, str] | None = None,
) -> HTMLResponse:
    """목록 조각 하나. 더하기·고치기·켜기끄기·씨앗 넣기가 모두 이 조각으로 돌아온다."""
    counts = _job_counts(conn)
    return render(
        request,
        "fragments/industry_list.html",
        rows=[IndustryRow(item, counts.get(item.name, 0)) for item in industries.list_all(conn)],
        is_empty=industries.is_empty(conn),
        message=message,
        error=error,
    )


@router.get("/ui/industries", response_class=HTMLResponse)
def industry_list_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> HTMLResponse:
    return _list(request, conn)


@router.post("/ui/industries", response_class=HTMLResponse)
def create_industry_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    name: Annotated[str, Form()],
    sort_order: Annotated[int, Form()] = 0,
    note: Annotated[str, Form()] = "",
) -> HTMLResponse:
    try:
        created = industries.create(conn, name=name, sort_order=sort_order, note=note)
    except industries.IndustryError as exc:
        return _list(request, conn, error={"reason": exc.reason, "message": str(exc)})
    return _list(request, conn, message=f"산업 '{created.name}' 를 더했다")


@router.put("/ui/industries/{industry_id}", response_class=HTMLResponse)
def update_industry_fragment(
    request: Request,
    industry_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    name: Annotated[str, Form()],
    sort_order: Annotated[int, Form()] = 0,
    note: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """이름·순서·메모를 저장한다. 이름이 바뀌면 옛 이름으로 이미 분류된 공고 수를 함께 알린다."""
    existing = industries.read(conn, industry_id)
    if existing is None:
        return _list(
            request, conn, error={"reason": "not_found", "message": f"id {industry_id} 가 없다"}
        )
    old_count = _job_counts(conn).get(existing.name, 0)
    try:
        updated = industries.update(conn, industry_id, name=name, sort_order=sort_order, note=note)
    except industries.IndustryError as exc:
        return _list(request, conn, error={"reason": exc.reason, "message": str(exc)})

    if updated.name != existing.name and old_count > 0:
        message = (
            f"'{existing.name}' 를 '{updated.name}' 로 고쳤다. "
            f"'{existing.name}' 으로 이미 분류된 공고 {old_count}건은 새 이름과 어긋난다"
        )
    else:
        message = f"'{updated.name}' 를 저장했다"
    return _list(request, conn, message=message)


@router.post("/ui/industries/{industry_id}/toggle", response_class=HTMLResponse)
def toggle_industry_fragment(
    request: Request,
    industry_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> HTMLResponse:
    """켜짐·꺼짐만 뒤집는다. 지우는 라우트는 없다."""
    existing = industries.read(conn, industry_id)
    if existing is None:
        return _list(
            request, conn, error={"reason": "not_found", "message": f"id {industry_id} 가 없다"}
        )
    updated = industries.set_enabled(conn, industry_id, not existing.enabled)
    state = "켰다" if updated.enabled else "껐다"
    return _list(request, conn, message=f"'{updated.name}' 를 {state}")


@router.post("/ui/industries/seed", response_class=HTMLResponse)
def seed_industries_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> HTMLResponse:
    added = industries.load_seed(conn, SEED_PATH)
    if added == 0:
        return _list(
            request,
            conn,
            error={
                "reason": "not_empty",
                "message": "표가 이미 비어 있지 않아 기본 산업을 다시 불러오지 않았다",
            },
        )
    return _list(request, conn, message=f"기본 산업 {added}개를 불러왔다")
