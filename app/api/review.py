"""공고 목록 화면. 수집한 공고를 좁혀 보고, 한 건을 오른쪽 패널로 열고, 잘못 들어온 것을 지운다.

위 메뉴의 첫 화면이다 (2026-09-17, LC-3344). 숫자 카드 셋(오늘 들어옴·확인 필요·오늘 보냄)과
수집이 실패한 사이트 알림이 목록 위에 있고, 목록은 칩으로 오늘 들어옴·확인 필요·보냄·전체를 고른다.

크롤러에서는 사람이 값을 고치지 않는다 (2026-09-15 결정). 예전의 수정 모달·보정·제안 수락과
완성 공고 화면을 이 한 목록으로 합쳤다. 상세 패널은 오공고로 보내는 칸을 빈 칸까지 모두 보이고,
보냈는지만 적는다 — 오공고에 들어간 뒤의 상태는 크롤러가 알 수 없다.

조회 조건과 지우기는 `app/api/review_filter.py` 다.

## 페이징은 오프셋 기반이다

제공 API(`app/api/jobs.py`)의 커서와 다르다. 저쪽은 폴링 사이에 삽입된 행 때문에 건너뛰는
건이 생기면 안 되고, 이쪽은 사람이 3페이지를 다시 열고 전체 페이지 수를 봐야 한다.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import custom_fields
from app.api import crawlers, job_detail
from app.api.review_filter import (
    DEADLINE_STATES,
    DUP_CRITERIA,
    DUP_GROUP_PREVIEW,
    DUP_LABELS,
    DUP_NOTES,
    FAILED_SQL,
    SENT_SQL,
    SHORT_BODY_SQL,
    UNCLASSIFIED_SQL,
    UNREADY_SQL,
    VIEW_ALL,
    VIEW_CHECK,
    VIEW_SENT,
    VIEW_TODAY,
    VIEWS,
    JobFilter,
    count,
    dup_columns,
    dup_groups,
    failing_sites,
    fill_rates,
    filter_sql,
    order_clause,
    read_filter,
    sent_today,
    view_counts,
    workflow_label,
)
from app.api.ui import display_zone, render, render_page
from app.normalize.engine import read_raw
from app.taxonomy import list_majors

router = APIRouter(tags=["ui"], include_in_schema=False)

# 한 페이지에 보여줄 행 수
PAGE_SIZE = 20

# 현재 페이지 주변으로 몇 개의 페이지 번호를 직접 누르게 둘지
PAGE_WINDOW = 2

_COLUMNS = f"""
    SELECT n.id AS id,
           n.raw_job_id AS raw_job_id,
           n.part AS part,
           n.company_name AS company_name,
           n.parent_company_name AS parent_company_name,
           n.title AS title,
           n.job_field AS job_field,
           n.job_role AS job_role,
           n.employment_type AS employment_type,
           n.experience_type AS experience_type,
           n.experience_min_years AS experience_min_years,
           n.recruitment_end_at AS recruitment_end_at,
           r.crawled_at AS crawled_at,
           w.name AS workflow_name,
           {SENT_SQL} AS sent,
           {FAILED_SQL} AS delivery_failed,
           {UNREADY_SQL} AS unready,
           {UNCLASSIFIED_SQL} AS unclassified,
           {SHORT_BODY_SQL} AS short_body
"""

_FROM = """
      FROM normalized_jobs n
      JOIN raw_jobs r ON r.id = n.raw_job_id
      JOIN workflows w ON w.id = r.workflow_id
"""


def d_day(recruitment_end_at: str | None) -> str | None:
    """마감까지 며칠인지. 못 읽으면(형식이 다르거나 없으면) None 이다."""
    if not recruitment_end_at:
        return None
    try:
        target = date.fromisoformat(recruitment_end_at.strip()[:10])
    except ValueError:
        return None
    delta = (target - datetime.now(display_zone()).date()).days
    if delta < 0:
        return "마감"
    if delta == 0:
        return "D-DAY"
    return f"D-{delta}"


def _page_url(criteria: dict[str, str], page: int) -> str:
    """페이지 이동 주소. 지금 걸린 조회 조건을 그대로 달고 페이지 번호만 바꾼다."""
    return "/ui/review?" + urlencode({**criteria, "page": page})


def _page_numbers(page: int, total_pages: int) -> list[int]:
    """현재 페이지 주변의 번호."""
    start = max(1, page - PAGE_WINDOW)
    end = min(total_pages, page + PAGE_WINDOW)
    return list(range(start, end + 1))


@router.get("/review", response_class=HTMLResponse)
def review_page(request: Request) -> HTMLResponse:
    return render_page(request, "pages/review.html")


@router.get("/complete")
def complete_page() -> RedirectResponse:
    """옛 완성 공고 주소. 공고 목록으로 합쳤다 (2026-09-15). 북마크가 죽지 않게 보낸다."""
    return RedirectResponse("/review", status_code=307)


@router.get("/ui/review", response_class=HTMLResponse)
def review_table_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
    picked: Annotated[JobFilter, Depends(read_filter)],
    page: int = 1,
) -> HTMLResponse:
    """공고 한 페이지와 AI 채움률. 조회 조건과 페이지 이동이 이 조각만 갈아 끼운다."""
    where, params = filter_sql(picked)
    total = count(conn, picked)
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    # 마지막 페이지 뒤를 요청하면 마지막 페이지를 준다. 빈 표를 주면 조건이 잘못됐다고 읽는다
    current = min(max(page, 1), total_pages)

    rows = conn.execute(
        f"{_COLUMNS}{dup_columns(picked.dup)}{_FROM}{where}"
        f"{order_clause(picked.dup)} LIMIT ? OFFSET ?",
        [*params, PAGE_SIZE, (current - 1) * PAGE_SIZE],
    ).fetchall()
    groups = dup_groups(conn, picked)
    group_numbers = {group["key"]: group["number"] for group in groups}
    listed = [
        {
            "job": row,
            "d_day": d_day(row["recruitment_end_at"]),
            "dup_group": group_numbers.get(str(row["dup_key"])) if picked.dup else None,
            "dup_size": int(row["dup_size"]) if picked.dup else 0,
            "marks": row_marks(row),
        }
        for row in rows
    ]
    criteria = picked.as_form()
    return render(
        request,
        "fragments/review_table.html",
        jobs=listed,
        fills=fill_rates(conn, picked),
        dup_picked=picked.dup,
        dup_label=DUP_LABELS.get(picked.dup, ""),
        dup_note=DUP_NOTES.get(picked.dup, ""),
        dup_groups=groups[:DUP_GROUP_PREVIEW],
        dup_group_count=len(groups),
        # 여분은 묶음마다 한 건씩 남기고 센 수다. 지울 수 있는 최대치이지 지울 건수가 아니다
        dup_extra=total - len(groups),
        total=total,
        page=current,
        total_pages=total_pages,
        first_index=(current - 1) * PAGE_SIZE + 1 if rows else 0,
        last_index=(current - 1) * PAGE_SIZE + len(rows),
        page_numbers=_page_numbers(current, total_pages),
        page_url=lambda number: _page_url(criteria, number),
        delete_criteria=criteria,
        # 워크플로우를 골랐을 때만 그 사이트의 수집분을 통째로 비우는 길이 열린다
        workflow=workflow_label(conn, picked.workflow_id),
        view=picked.view or VIEW_ALL,
        view_label=VIEWS.get(picked.view, VIEWS[VIEW_ALL]),
        empty_hint=_EMPTY_HINTS.get(picked.view, _EMPTY_HINTS[""]),
    )


# 목록이 비었을 때의 한 줄. 보기마다 비는 이유가 다르다
_EMPTY_HINTS: dict[str, str] = {
    VIEW_TODAY: "오늘 들어온 공고가 없다. 사이트 화면에서 수집이 돌고 있는지 확인한다",
    VIEW_CHECK: "확인이 필요한 공고가 없다",
    VIEW_SENT: "아직 오공고로 보낸 공고가 없다",
    "": (
        "조건에 맞는 공고가 없다. 조회 조건을 넓히거나, 워크플로우가 한 번이라도 실행됐는지"
        " 확인한다"
    ),
}


def row_marks(job: sqlite3.Row) -> list[tuple[str, str]]:
    """행에 붙일 상태 표시 (낱말, 색). 보낸 공고는 `오공고 보냄` 하나다.

    AI 분류가 안 된 공고는 필수 칸도 당연히 비어 있어 `필수 칸 빔` 을 겹쳐 적지 않는다.
    """
    if job["sent"]:
        return [("오공고 보냄", "ok")]
    marks: list[tuple[str, str]] = []
    if job["delivery_failed"]:
        marks.append(("전송 실패", "bad"))
    if job["unclassified"]:
        marks.append(("AI 분류 안 됨", "warn"))
    elif job["unready"]:
        marks.append(("필수 칸 빔", "warn"))
    if job["short_body"]:
        marks.append(("본문 짧음", "warn"))
    return marks or [("보내기 전", "idle")]


@router.get("/ui/review/summary", response_class=HTMLResponse)
def review_summary_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    """목록 위 숫자 카드 셋과 수집 실패 알림. 카드를 누르면 그 보기로 목록이 바뀐다."""
    counts = view_counts(conn)
    return render(
        request,
        "fragments/review_summary.html",
        today=counts[VIEW_TODAY],
        check=counts[VIEW_CHECK],
        sent_today=sent_today(conn),
        failing=failing_sites(conn),
    )


@router.get("/ui/review/filters", response_class=HTMLResponse)
def review_filters_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    """조회 조건. 사이트는 워크플로우, 직군은 켜진 직무 분류 대분류에서 만든다."""
    workflows = conn.execute("SELECT id, name FROM workflows ORDER BY id").fetchall()
    return render(
        request,
        "fragments/review_filters.html",
        workflows=workflows,
        job_majors=[major.name for major in list_majors(conn, enabled_only=True)],
        deadline_states=DEADLINE_STATES,
        views=VIEWS,
        view_counts=view_counts(conn),
        default_view=VIEW_TODAY,
        dup_criteria=DUP_CRITERIA,
        dup_labels=DUP_LABELS,
    )


def _panel_row(conn: sqlite3.Connection, normalized_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"""
        SELECT n.*, w.name AS workflow_name, r.crawled_at AS crawled_at,
               COALESCE(sub.logo_url, par.logo_url) AS logo_url,
               {SENT_SQL} AS sent,
               {FAILED_SQL} AS delivery_failed,
               {UNREADY_SQL} AS unready,
               {UNCLASSIFIED_SQL} AS unclassified,
               {SHORT_BODY_SQL} AS short_body
          FROM normalized_jobs n
          JOIN raw_jobs r ON r.id = n.raw_job_id
          JOIN workflows w ON w.id = r.workflow_id
          LEFT JOIN companies sub ON sub.name = NULLIF(n.company_name, '')
          LEFT JOIN companies par ON par.name = n.parent_company_name
         WHERE n.id = ?
        """,
        (normalized_id,),
    ).fetchone()


def render_panel(
    request: Request,
    conn: sqlite3.Connection,
    normalized_id: int,
    *,
    editing: bool = False,
    message: str = "",
) -> HTMLResponse:
    """공고 한 건의 패널. 보기와 고치기가 같은 조각이다 (`app/api/job_detail.py`)."""
    row = _panel_row(conn, normalized_id)
    if row is None:
        return render(request, "fragments/job_panel.html", job=None, normalized_id=normalized_id)
    edited = job_detail.overrides(conn, int(row["raw_job_id"]), int(row["part"]))
    classified = job_detail.classification(conn, row)
    _, raw = read_raw(conn, int(row["raw_job_id"]))
    ai_filled = job_detail.ai_filled_fields(raw, row)
    return render(
        request,
        "fragments/job_panel.html",
        job=row,
        normalized_id=normalized_id,
        d_day=d_day(row["recruitment_end_at"]),
        marks=row_marks(row),
        sent=bool(row["sent"]),
        delivered=job_detail.delivery(conn, str(row["source_url"])),
        classified=classified,
        parts=job_detail.parts_of(conn, int(row["raw_job_id"])),
        sections=job_detail.SECTIONS,
        sources={
            field.name: job_detail.source_of(field.name, edited, classified is not None, ai_filled)
            for field in job_detail.FIELDS
        },
        source_labels=job_detail.SOURCE_LABELS,
        field_display=job_detail.display,
        editing=editing,
        list_choices=job_detail.list_choices(conn) if editing else None,
        message=message,
        custom_values=custom_fields.values_for(conn, int(row["raw_job_id"]), int(row["part"])),
    )


@router.get("/ui/review/jobs/{normalized_id}/panel", response_class=HTMLResponse)
def job_panel_fragment(
    request: Request,
    normalized_id: int,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    """공고 한 건의 오른쪽 패널.

    나눈 공고는 번호마다 따로 연다 — 정규화 행 id 로 찾으므로 형제 공고가 섞이지 않는다.
    로고는 자회사 로고가 먼저이고 없으면 모회사 로고다.
    """
    return render_panel(request, conn, normalized_id)
