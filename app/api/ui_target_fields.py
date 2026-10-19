"""공고 목록의 직군 칸에서 수집·전송 대상 직군을 켜고 끈다 (2026-10-19, LC-3444).

직군 칸을 펼치면 직군마다 이름과 스위치가 한 줄이다. 이름을 누르면 지금처럼 그 직군으로 조회하고,
스위치를 누르면 그 직군을 수집·자동 전송 대상에서 켜거나 끈다. 다음 수집 실행부터 적용된다
(`app/crawler/target_fields.py`).

스위치 하나를 누를 때마다 바로 저장한다. 저장 단추를 따로 두면 켜 놓고 저장하지 않은 채 목록을
떠나기 쉽다. 응답은 스위치 목록 전체다 — 하나를 끄지 못한 경우(모두 끄기)에도 화면이 저장된 값과
같아야 한다.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.api import crawlers
from app.api.ui import render
from app.crawler import target_fields
from app.taxonomy import list_majors

router = APIRouter(tags=["ui"], include_in_schema=False)


def field_names(conn: sqlite3.Connection) -> list[str]:
    """직군 칸에 보일 직군. 켜진 직무 분류 대분류다."""
    return [major.name for major in list_majors(conn, enabled_only=True)]


def switch_context(conn: sqlite3.Connection) -> dict[str, object]:
    """직군 줄을 그리는 값. 조회 조건 조각도 같이 쓴다 (`app/api/review.py`)."""
    return {
        "names": field_names(conn),
        "switchable": set(target_fields.choices()),
        "on": set(target_fields.read_fields(conn)),
    }


def render_switches(request: Request, conn: sqlite3.Connection, *, error: str = "") -> HTMLResponse:
    # 직군 칸 이름 옆 수집 직군 수도 같이 바꾼다 (`hx-swap-oob`)
    return render(
        request, "fragments/target_field_rows.html", **switch_context(conn), error=error, oob=True
    )


@router.post("/ui/target-fields", response_class=HTMLResponse)
def toggle_target_field(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
    field: Annotated[str, Form()],
    on: Annotated[bool, Form()],
) -> HTMLResponse:
    """직군 하나를 켜거나 끈다. 거절되면 저장하지 않고 사유를 목록 위에 적는다."""
    error = ""
    try:
        target_fields.set_field(conn, field, on)
    except target_fields.TargetFieldError as exc:
        error = str(exc)
    return render_switches(request, conn, error=error)
