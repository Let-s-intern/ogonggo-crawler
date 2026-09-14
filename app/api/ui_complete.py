"""완성 공고 화면. 정규화·분류가 열여섯 칸 중 60% 이상을 채운 공고만 보여준다.

운영자가 "분류까지 다 끝나서 데이터가 거의 빠짐없는 것" 만, 소비 측(잡보드)에 나갈 모양
그대로 미리 훑어보고 싶을 때 쓰는 자리다. 검수 화면(`/review`)은 무엇을 고칠지 찾는
화면이고, 여기는 고치는 기능이 없다 — 로고까지 붙여 실제로 어떻게 보일지 확인만 한다.

## "완성" 은 열여섯 칸 중 60% 이상(열 칸 이상)이 채워졌다는 뜻이다

`app/normalize/rules.py` 의 `NORMALIZED_FIELDS` 열여섯 칸(수집이 채우는 것, 분류가 채우는 것, 직무
분류 둘 포함) 중 값이 있는 칸 수를 세어 임계치(`_THRESHOLD`) 이상이면 완성으로 본다. 사이트에 따라
`preferred_qualifications`·`hiring_process`·`recruitment_notice` 처럼 빈 것이 정상인 칸이
있어서(`app/api/review_filter.py` 의 `EMPTY_NOTES`) 열여섯 칸 전부를 요구하면 통과하는 건이 지나치게
적어진다 — 2026-08-29 운영자 요청으로 100% -> 80% -> 60% 로 두 번 낮췄다.

사람이 고친 값(`job_field_overrides`)은 여기서 보지 않는다. 목록·상세 모두
`normalized_jobs` 원 컬럼만 본다 — 검수해서 고친 값을 보려면 `/review`로 간다.

## 로고는 자회사 우선, 없으면 모회사로 떨어진다

`companies` 행은 자회사·모회사 이름으로 각각 따로 생긴다(`app/companies.py::register`).
자회사 이름의 행에 로고가 없어도 모회사 이름의 행에는 있을 수 있다 — 계열사 로고를 하나하나
안 올려도 모회사 로고가 대신 보이는 편이 운영자에게 유용해서, 자회사·모회사 두 표를 각각
조인해 `COALESCE(sub.logo_url, par.logo_url)` 로 고른다. 회사명 자체를 합치지 않는다 —
자회사가 있으면 자회사 이름을 그대로 보여주고, 로고만 대신 채운다.

## 목록은 커서로, 상세는 이 화면 전용 읽기 전용 미리보기다

무한스크롤이라 오프셋이 아니라 `id` 커서를 쓴다 — 스크롤 중 새 공고가 쌓여도 이미 본 행이
밀리거나 중복되지 않는다. 상세(`GET /ui/complete/{id}/preview`)는 검수 모달을 재사용하지
않는다 — 이 화면은 고치는 기능이 필요 없고, 소비 측 화면처럼 섹션으로 나눠 읽기 좋게 보여
주는 것이 목적이라 모양 자체가 다르다.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from app.api.settings import get_connection
from app.api.ui import render, render_page
from app.normalize.rules import NORMALIZED_FIELDS

router = APIRouter(tags=["ui"], include_in_schema=False)

# 한 번에 불러오는 건수. 너무 크면 스크롤 한 번에 다 오고, 너무 작으면 요청이 잦다
PAGE_SIZE = 20

# 완성으로 볼 최소 채움 비율. 100% 는 사이트별로 정상적으로 비는 칸 때문에 통과하는 건이
# 거의 없어서 80% 로, 그래도 적어 60% 로 낮췄다(2026-08-29)
_THRESHOLD = 0.6
_REQUIRED_FILLED = math.ceil(len(NORMALIZED_FIELDS) * _THRESHOLD)

# 채워진 칸 수를 세는 SQL 조각. 열여섯 칸 중 이 값이 _REQUIRED_FILLED 이상이면 완성이다
_FILLED_COUNT_SQL = " + ".join(
    f"(CASE WHEN {name} IS NOT NULL AND trim({name}) != '' THEN 1 ELSE 0 END)"
    for name in NORMALIZED_FIELDS
)
_COMPLETE_WHERE = f"({_FILLED_COUNT_SQL}) >= {_REQUIRED_FILLED}"

# 자회사·모회사 각각의 로고 표를 조인해 자회사 우선으로 고른다. 두 조인의 별칭(sub/par)은
# 아래 SELECT 의 COALESCE(sub.logo_url, par.logo_url) 와 짝이 맞아야 한다
_LOGO_JOIN_SQL = """
          LEFT JOIN companies sub ON sub.name = NULLIF(n.company_name, '')
          LEFT JOIN companies par ON par.name = n.parent_company_name
"""


def _d_day(recruitment_end_at: str | None) -> str | None:
    """마감까지 며칠인지. 못 읽으면(형식이 다르거나 없으면) None 이다."""
    if not recruitment_end_at:
        return None
    try:
        target = date.fromisoformat(recruitment_end_at.strip()[:10])
    except ValueError:
        return None
    delta = (target - date.today()).days
    if delta < 0:
        return "마감"
    if delta == 0:
        return "D-DAY"
    return f"D-{delta}"


def _rows(
    conn: sqlite3.Connection, after: int | None, *, limit: int = PAGE_SIZE
) -> list[sqlite3.Row]:
    params: list[object] = []
    where = _COMPLETE_WHERE
    if after is not None:
        where += " AND n.id < ?"
        params.append(after)
    params.append(limit)
    return conn.execute(
        f"""
        SELECT n.id, n.raw_job_id, n.parent_company_name, n.company_name, n.title,
               n.job_field, n.job_role, n.employment_type, n.experience_type,
               n.region, n.recruitment_end_at, n.source_url,
               COALESCE(sub.logo_url, par.logo_url) AS logo_url
          FROM normalized_jobs n
          {_LOGO_JOIN_SQL}
         WHERE {where}
         ORDER BY n.id DESC
         LIMIT ?
        """,
        params,
    ).fetchall()


def completed_count(conn: sqlite3.Connection) -> int:
    """지금까지 완성으로 판정된 공고 수. 대시보드 지표가 이 값을 그대로 쓴다."""
    row = conn.execute(
        f"SELECT count(*) AS n FROM normalized_jobs n WHERE {_COMPLETE_WHERE}"
    ).fetchone()
    return int(row["n"])


def recent_completed(conn: sqlite3.Connection, limit: int) -> list[dict[str, object]]:
    """가장 최근 완성 공고. 목록 카드(`cards`)와 같은 모양이라 대시보드가 같은 카드
    마크업을 그대로 재사용할 수 있다."""
    rows = _rows(conn, None, limit=limit)
    return [{"row": row, "d_day": _d_day(row["recruitment_end_at"])} for row in rows]


def _read_detail(conn: sqlite3.Connection, normalized_id: int) -> sqlite3.Row | None:
    return conn.execute(
        f"""
        SELECT n.*, COALESCE(sub.logo_url, par.logo_url) AS logo_url
          FROM normalized_jobs n
          {_LOGO_JOIN_SQL}
         WHERE n.id = ?
        """,
        (normalized_id,),
    ).fetchone()


@router.get("/complete", response_class=HTMLResponse)
def complete_page(request: Request) -> HTMLResponse:
    return render_page(request, "pages/complete.html")


@router.get("/ui/complete", response_class=HTMLResponse)
def complete_list_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    after: Annotated[int | None, Query()] = None,
) -> HTMLResponse:
    """공고 한 묶음. 마지막 행에 다음 묶음을 불러오는 감지기가 붙는다.

    더 가져올 것이 없으면 감지기를 붙이지 않는다 — 붙이면 스크롤이 바닥에 닿을 때마다
    같은 빈 요청을 반복한다.
    """
    rows = _rows(conn, after)
    cards = [{"row": row, "d_day": _d_day(row["recruitment_end_at"])} for row in rows]
    next_after = int(rows[-1]["id"]) if len(rows) == PAGE_SIZE else None
    return render(
        request,
        "fragments/complete_list.html",
        cards=cards,
        next_after=next_after,
        is_first_page=after is None,
    )


@router.get("/ui/complete/{normalized_id}/preview", response_class=HTMLResponse)
def complete_preview_fragment(
    request: Request,
    normalized_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> HTMLResponse:
    """공고 한 건을 소비 측이 볼 모양대로 읽기 전용으로 보여준다. 고치는 자리가 없다."""
    row = _read_detail(conn, normalized_id)
    if row is None:
        # 다른 `/ui/*` 실패와 같은 규칙이다 — 조각은 200으로 돌아오고 안에 사유를 적는다.
        # HTMX 스왑은 상태 코드가 아니라 본문을 그대로 자리에 끼운다
        return render(request, "fragments/complete_not_found.html", normalized_id=normalized_id)
    return render(
        request,
        "fragments/complete_preview.html",
        job=row,
        d_day=_d_day(row["recruitment_end_at"]),
    )
