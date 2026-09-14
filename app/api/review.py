"""데이터 검수 화면의 페이지·목록·편집 모달.

사람이 수집 결과를 보고 틀린 값을 고치는 자리다. Push 30 에서 데이터 조회(`/jobs`)를 여기로
합쳤다 — 두 화면이 같은 데이터를 두 벌로 보여주면서 목록·상세 모달·시각 표시가 겹쳤다.
한 화면에서 좁혀서 보고, 고치고, 좁힌 것을 지운다.

조회 조건과 지우기는 `app/api/review_filter.py` 다. 조건을 만드는 곳이 하나여야 표가 센
건수와 지우기가 지우는 행이 같다.

## 페이징은 오프셋 기반이다

제공 API(`app/api/jobs.py`)의 커서와 다르다. 저쪽은 폴링 사이에 삽입된 행 때문에 건너뛰는
건이 생기면 안 되고, 이쪽은 사람이 3페이지를 다시 열고 전체 페이지 수를 봐야 한다. 의도된
차이다.

## 기본 정렬은 미전달 우선이다

이미 전달된 행을 고쳐도 소비 측이 가진 값은 바뀌지 않는다. 수동 수정은 `delivered_at` 을
지우거나 되돌리지 않기 때문이다 (`.claude/rules/data-safety.md`). 그래서 검수는 전달 전에
하는 것이 정상 경로고, 고르지 않으면 화면이 그 순서로 행을 내놓는다.

## 이 파일은 `normalized_jobs` 를 쓰지 않는다

사람이 고친 값은 `job_field_overrides` 에만 쌓인다. 확정 값은 규칙과 보정에서 매번 다시
만들어지는 파생값이고, 파생값에 손으로 쓰면 다음 재정규화가 그것을 덮어쓴다 (Push 10 의 전제,
`migrations/0005_job_field_overrides.sql`).

표가 보여주는 값은 그래서 두 겹이다. 보정이 있으면 사람이 정한 값을, 없으면 규칙이 만든 값을
보여주고 어느 쪽인지 단어로 적는다. `normalized_jobs` 컬럼 자체는 다음 정규화에서 갱신된다.
빈 값 조건도 같은 두 겹을 본다 (`review_filter.empty_condition`) — 사람이 채운 필드가 계속
`빈 값` 으로 걸리면 검수한 것이 검수 대상에 남는다.
"""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.api import crawlers
from app.api.review_filter import (
    DEADLINE_STATES,
    DEFAULT_SORT,
    DELIVERY_STATES,
    DUP_CRITERIA,
    DUP_GROUP_PREVIEW,
    DUP_LABELS,
    DUP_NOTES,
    EMPTY_CHOICES,
    EMPTY_LABELS,
    FIELD_LABELS,
    HAS_SUGGESTION_STATES,
    SORT_LABELS,
    JobFilter,
    count,
    dup_columns,
    dup_groups,
    empty_counts,
    filter_sql,
    order_clause,
    read_filter,
    workflow_label,
)
from app.api.ui import render, render_page
from app.classify.schema import JUDGE_FIELDS, NUMBER_FIELDS, TAXONOMY_FIELDS, VALUE_LABELS
from app.classify.store import read_evidence, read_suggestions, read_suggestions_batch
from app.crawler.collect import API
from app.normalize.engine import OVERRIDABLE_FIELDS
from app.taxonomy import list_majors

router = APIRouter(tags=["ui"], include_in_schema=False)

# 한 페이지에 보여줄 행 수. 운영자가 고를 수 있는 값만 받는다
PAGE_SIZES: tuple[int, ...] = (20, 50, 100)
DEFAULT_PAGE_SIZE = 20

# 현재 페이지 주변으로 몇 개의 페이지 번호를 직접 누르게 둘지
PAGE_WINDOW = 2

# 여러 줄로 들어오는 필드. 한 줄 입력으로 고치면 줄바꿈이 사라진다
LONG_FIELDS: frozenset[str] = frozenset(
    {
        "body",
        "qualifications",
        "responsibilities",
        "preferred_qualifications",
        "hiring_process",
        "recruitment_notice",
        "company_and_team_introduction",
        "compensation",
        "benefits",
    }
)

# 모달을 닫으라고 화면에 알리는 이벤트 이름. `base.html` 의 여닫는 스크립트가 이것을 듣는다
MODAL_DONE_EVENT = "app-modal-done"

_COLUMNS = """
    SELECT n.id            AS id,
           n.raw_job_id    AS raw_job_id,
           n.part          AS part,
           n.parent_company_name AS parent_company_name,
           n.company_name       AS company_name,
           n.title         AS title,
           n.recruitment_end_at      AS recruitment_end_at,
           n.body          AS body,
           n.qualifications  AS qualifications,
           n.recruitment_start_at    AS recruitment_start_at,
           n.employment_type AS employment_type,
           n.experience_type  AS experience_type,
           n.region AS region,
           n.responsibilities        AS responsibilities,
           n.preferred_qualifications     AS preferred_qualifications,
           n.hiring_process AS hiring_process,
           n.recruitment_notice      AS recruitment_notice,
           n.job_field     AS job_field,
           n.job_role     AS job_role,
           n.company_and_team_introduction AS company_and_team_introduction,
           n.compensation AS compensation,
           n.benefits AS benefits,
           n.education_level AS education_level,
           n.recruitment_headcount AS recruitment_headcount,
           n.experience_min_years AS experience_min_years,
           n.closes_when_filled AS closes_when_filled,
           n.application_method AS application_method,
           n.recruitment_type AS recruitment_type,
           n.auto_close_enabled AS auto_close_enabled,
           n.source_url    AS source_url,
           n.normalized_at AS normalized_at,
           n.delivered_at  AS delivered_at,
           r.crawled_at    AS crawled_at,
           r.content_hash  AS content_hash,
           r.workflow_id   AS workflow_id,
           w.name          AS workflow_name
"""

_FROM = """
      FROM normalized_jobs n
      JOIN raw_jobs r ON r.id = n.raw_job_id
      JOIN workflows w ON w.id = r.workflow_id
"""

_BASE = f"{_COLUMNS}{_FROM}"


def _page_url(criteria: dict[str, str], page: int) -> str:
    """페이지 이동 주소. 지금 걸린 조회 조건을 그대로 달고 페이지 번호만 바꾼다.

    조건을 서버가 붙여 두면 페이지 버튼이 폼을 참조하지 않아도 된다. 참조하게 두면 조건을
    바꾸고 조회를 누르지 않은 상태에서 페이지를 넘길 때, 화면에 보이는 표와 다른 조건으로
    넘어간다.
    """
    return "/ui/review?" + urlencode({**criteria, "page": page})


def _page_numbers(page: int, total_pages: int) -> list[int]:
    """현재 페이지 주변의 번호. 3페이지를 다시 여는 것이 한 번에 되게 한다."""
    start = max(1, page - PAGE_WINDOW)
    end = min(total_pages, page + PAGE_WINDOW)
    return list(range(start, end + 1))


# 분류가 근거 문장과 함께 고르는 칸. 근거를 찾지 못해도 값은 남아 검수 화면이 먼저 보여야 한다
# (`app/classify/grounding.py`)
_JUDGED_FIELDS: frozenset[str] = frozenset((*JUDGE_FIELDS, *NUMBER_FIELDS, *TAXONOMY_FIELDS))


def _cell(
    job: sqlite3.Row,
    field: str,
    overrides: dict[str, str],
    suggestions: dict[str, dict[str, str]] | None = None,
    evidence: dict[str, str] | None = None,
) -> dict[str, Any]:
    """셀 하나가 그려지는 데 필요한 전부.

    `rule_value` 는 `normalized_jobs` 컬럼, 즉 규칙이 만든 값이다. 보정이 있으면 화면에 나가는
    값은 사람이 정한 값이고, 규칙값은 무엇에서 고쳤는지 보이도록 함께 남긴다.

    보정 여부를 값의 참·거짓으로 판정하지 않는다. 빈 문자열은 "이 필드는 비어 있는 것이 맞다"
    는 사람의 판단이고, 보정이 없는 것과 다르다 (`migrations/0005_job_field_overrides.sql`).

    `suggestions` 는 `job_field_suggestions` 에 남아 있는 값이다(`app/classify/store.py` 의
    `read_suggestions`). 보정과 독립이다 — 값이 이미 사람 보정으로 확정돼 있으면서 동시에
    새 제안이 붙어 있을 수 있어(원문을 다시 읽었더니 보정한 값과도 다른 경우), 둘 다 보여준다
    (11.6, PRD 6절).
    """
    overridden = field in overrides
    rule_value = job[field] if field in job.keys() else None
    suggestion = (suggestions or {}).get(field)
    return {
        "job_id": int(job["id"]),
        "field": field,
        "label": FIELD_LABELS[field],
        "rule_value": rule_value,
        "value": overrides[field] if overridden else rule_value,
        "overridden": overridden,
        "long": field in LONG_FIELDS,
        "suggested": suggestion is not None,
        "suggestion_value": suggestion["value"] if suggestion else "",
        "suggestion_reason": suggestion["reason"] if suggestion else "",
        # 판정 값은 목록에서 고른다. 화면 이름과 저장 이름의 표다 (2026-09-14 결정)
        "choices": VALUE_LABELS.get(field),
        # 분류가 고른 칸이면 근거 문장을 함께 보인다. 없으면 `근거 없음` 이다
        "judged": field in _JUDGED_FIELDS,
        "evidence": (evidence or {}).get(field, ""),
    }


def _read_overrides(
    conn: sqlite3.Connection, postings: list[tuple[int, int]]
) -> dict[tuple[int, int], dict[str, str]]:
    """여러 공고의 보정을 한 번에 읽는다. 행마다 따로 물으면 한 페이지에 쿼리가 수십 개 붙는다.

    키는 (수집 건, 번호) 다. 나눈 공고는 수집 건 하나에 공고가 여럿이고 보정도 번호마다 따로다.
    """
    if not postings:
        return {}
    raw_job_ids = sorted({raw_job_id for raw_job_id, _ in postings})
    marks = ",".join("?" for _ in raw_job_ids)
    rows = conn.execute(
        f"SELECT raw_job_id, part, field_name, value FROM job_field_overrides"
        f" WHERE raw_job_id IN ({marks})",
        raw_job_ids,
    ).fetchall()
    found: dict[tuple[int, int], dict[str, str]] = {}
    for row in rows:
        key = (int(row["raw_job_id"]), int(row["part"]))
        found.setdefault(key, {})[str(row["field_name"])] = str(row["value"])
    return found


def _posting_key(job: sqlite3.Row) -> tuple[int, int]:
    """보정과 제안이 걸리는 자리. 화면은 공고 번호로 가리키고, 저장은 (수집 건, 번호) 로 한다."""
    return int(job["raw_job_id"]), int(job["part"])


def _read_source(conn: sqlite3.Connection, raw_job_id: int) -> dict[str, Any]:
    """그 수집 건의 원문. `raw_jobs.raw_data_json` 의 `source_text` 키다 (side Push 8).

    `_COLUMNS` 에 넣지 않는다. 원문은 `normalized_jobs` 의 칸이 아니고, 표는 원문을 보여주지
    않는데 한 페이지 100건의 상세 전문을 함께 실어 오게 된다. 모달을 열 때 한 건만 읽는다.

    JSON 은 파이썬에서 푼다. `json_extract` 는 값이 JSON 이 아니면 그 자리에서 실패하고,
    그러면 원문 하나 때문에 모달 전체가 열리지 않는다 — 원문이 없는 것은 화면이 말할 수 있는
    상태이고, 모달이 안 열리는 것은 아니다.

    크롤러의 상세 경로를 함께 낸다. 원문이 없는 까닭이 경로마다 달라 화면이 갈라 적는다.
    상세가 API 인 사이트는 응답 전체를 편 원문을 붙이기 전에 모은 건이고, 수집은 아는 주소의
    상세를 다시 열지 않으므로 다시 수집해도 붙지 않는다. 나머지는 원문을 뽑기 전에 모은 건이다.
    """
    row = conn.execute(
        """
        SELECT r.raw_data_json  AS raw_data_json,
               c.detail_mode    AS detail_mode
          FROM raw_jobs r
          LEFT JOIN workflows w ON w.id = r.workflow_id
          LEFT JOIN crawlers c ON c.id = w.crawler_id
         WHERE r.id = ?
        """,
        (raw_job_id,),
    ).fetchone()
    if row is None:
        return {"text": "", "api_detail": False}
    api_detail = str(row["detail_mode"] or "") == API
    try:
        data = json.loads(str(row["raw_data_json"]))
    except (TypeError, ValueError):
        return {"text": "", "api_detail": api_detail}
    text = data.get("source_text") if isinstance(data, dict) else None
    return {
        "text": str(text) if isinstance(text, str) else "",
        "api_detail": api_detail,
    }


def _upsert_override(
    conn: sqlite3.Connection, raw_job_id: int, field: str, value: str, part: int = 1
) -> None:
    """`job_field_overrides` 에 값 하나를 넣거나 덮는다.

    `save_review_job_fragment` 의 저장과 제안 수락(11.6) 이 같은 문장을 쓴다 — 사람이 손으로
    고친 값과 제안을 수락해 만든 값은 같은 표의 같은 자리이지, 다른 경로가 아니다.
    """
    conn.execute(
        """
        INSERT INTO job_field_overrides (raw_job_id, part, field_name, value)
             VALUES (?, ?, ?, ?)
        ON CONFLICT (raw_job_id, part, field_name)
          DO UPDATE SET value = excluded.value, updated_at = datetime('now')
        """,
        (raw_job_id, part, field, value),
    )


def _read_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    """그 공고의 확정 행. 공고 번호(`normalized_jobs.id`)로 찾는다.

    수집 건으로 찾지 않는다. 나눈 공고는 수집 건 하나에 행이 여럿이라, 수집 건으로 찾으면 다른
    직무의 공고가 열린다 (2026-09-11 결정).
    """
    return conn.execute(f"{_BASE} WHERE n.id = ?", (job_id,)).fetchone()


def _text(value: Any) -> str:
    """DB 값이든 폼 값이든 비교할 수 있는 한 가지 문자열로.

    `NULL` 과 빈 문자열을 같은 것으로 본다. "값이 없다" 는 상태가 둘로 갈려 있으면, 아무것도
    고치지 않은 필드에 빈 보정이 생긴다.

    줄바꿈도 맞춘다. 브라우저는 `textarea` 를 CRLF 로 보내고 DB 에는 LF 로 들어 있어, 손대지
    않은 본문이 그대로 돌아와도 다른 값으로 읽힌다 — 그러면 저장을 누를 때마다 본문·자격요건에
    보정이 하나씩 생긴다.
    """
    if value is None:
        return ""
    return str(value).replace("\r\n", "\n").replace("\r", "\n")


def _modal_response(
    request: Request,
    conn: sqlite3.Connection,
    job_id: int,
    *,
    saved: str = "",
    error: str = "",
    note: str = "",
    drafts: dict[str, str] | None = None,
    focus_field: str = "",
    changed_fields: tuple[str, ...] = (),
    swap_row: bool = False,
    close: bool = False,
) -> HTMLResponse:
    """공고 한 건을 통째로 그리는 모달.

    필드 하나만 여는 경로는 두지 않는다. 값 하나만 보고는 그 값이 맞는지 판정할 수 없어서
    운영자가 같은 건을 여섯 번 열게 된다 — 검수는 한 건을 통째로 보는 일이다.

    저장·삭제 뒤에는 표의 그 행도 같은 응답에 실어 보낸다. 표를 다시 그리지 않는다.
    `swap_row` 가 켜지면 그 행의 값 칸 여섯, 보정 개수, 전달 칸만 OOB 로 갈린다.

    실패도 이 조각으로 나간다. 고치다 실패했는데 표 전체가 오류 상자로 바뀌면 운영자는 방금
    어디를 고치고 있었는지부터 다시 찾아야 한다.
    """
    job = _read_job(conn, job_id)
    if job is None:
        return render(
            request,
            "fragments/review_modal.html",
            job=None,
            fields=[],
            message=f"공고 {job_id} 의 정규화 행이 없다. 목록을 다시 불러 확인한다",
        )
    raw_job_id, part = _posting_key(job)
    overrides = _read_overrides(conn, [(raw_job_id, part)]).get((raw_job_id, part), {})
    suggestions = read_suggestions(conn, raw_job_id, part)
    evidence = read_evidence(conn, raw_job_id, part)
    response = render(
        request,
        "fragments/review_modal.html",
        job=job,
        source=_read_source(conn, raw_job_id),
        fields=[
            _cell(job, field, overrides, suggestions, evidence) for field in OVERRIDABLE_FIELDS
        ],
        override_count=len(overrides),
        drafts=drafts or {},
        focus_field=focus_field,
        changed_fields=changed_fields,
        saved=saved,
        error=error,
        note=note,
        swap_row=swap_row,
        message="",
    )
    if close:
        # 설정(settle)까지 끝난 뒤에 닫는다. 먼저 닫으면 표가 갈리기 전 화면이 드러난다
        response.headers["HX-Trigger-After-Settle"] = MODAL_DONE_EVENT
    return response


@router.get("/review", response_class=HTMLResponse)
def review_page(request: Request) -> HTMLResponse:
    return render_page(request, "pages/review.html")


@router.get("/ui/review", response_class=HTMLResponse)
def review_table_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
    picked: Annotated[JobFilter, Depends(read_filter)],
    sort: str = DEFAULT_SORT,
    order: str = "desc",
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> HTMLResponse:
    """검수 대상 한 페이지. 표 영역만 이 조각으로 갈린다.

    조건은 `review_filter.read_filter` 한 곳에서 읽는다. 표가 A 로 세고 지우기가 B 로 지우면
    화면에 적힌 건수가 거짓이 된다.

    필드별 빈 건수를 표 위에 함께 낸다. 조건을 걸기 전에 어디가 문제인지 보이지 않으면,
    한 필드만 놓친 셀렉터를 찾는 방법이 148건을 눈으로 훑는 것밖에 없다.

    조회 조건이 폼에서 올 때는 `page` 가 함께 오지 않아 1페이지가 된다. 조건을 바꿨는데 2페이지
    자리가 유지되면 사람이 보고 있는 것과 다른 구간이 나온다.
    """
    size = page_size if page_size in PAGE_SIZES else DEFAULT_PAGE_SIZE
    where, params = filter_sql(picked)

    total = count(conn, picked)
    total_pages = max(1, math.ceil(total / size))
    # 마지막 페이지 뒤를 요청하면 마지막 페이지를 준다. 빈 표를 주면 사람은 조건이 잘못됐다고
    # 읽는다
    current = min(max(page, 1), total_pages)

    rows = conn.execute(
        f"{_COLUMNS}{dup_columns(picked.dup)}{_FROM}{where}"
        f"{order_clause(sort, order, picked.dup)} LIMIT ? OFFSET ?",
        [*params, size, (current - 1) * size],
    ).fetchall()
    # 묶음 번호는 목록과 같은 순서로 매긴 것을 행에 되붙인다. 표에 `3번 묶음 5건` 이라고
    # 적혀야 페이지가 갈려도 짝이 어디 있는지 찾을 수 있다
    groups = dup_groups(conn, picked)
    group_numbers = {group["key"]: group["number"] for group in groups}
    postings = [_posting_key(row) for row in rows]
    overrides = _read_overrides(conn, postings)
    suggestions = read_suggestions_batch(conn, postings)
    listed = [
        {
            "job": row,
            "cells": [
                _cell(
                    row,
                    field,
                    overrides.get(_posting_key(row), {}),
                    suggestions.get(_posting_key(row), {}),
                )
                for field in OVERRIDABLE_FIELDS
            ],
            "override_count": len(overrides.get(_posting_key(row), {})),
            "dup_group": group_numbers.get(str(row["dup_key"])) if picked.dup else None,
            "dup_size": int(row["dup_size"]) if picked.dup else 0,
        }
        for row in rows
    ]

    criteria = {
        **picked.as_form(),
        "sort": sort if sort in SORT_LABELS else DEFAULT_SORT,
        "order": order if order in ("asc", "desc") else "desc",
        "page_size": str(size),
    }
    return render(
        request,
        "fragments/review_table.html",
        jobs=listed,
        fields=OVERRIDABLE_FIELDS,
        labels=FIELD_LABELS,
        empties=empty_counts(conn, picked),
        empty_picked=picked.empty,
        empty_labels=EMPTY_LABELS,
        dup_picked=picked.dup,
        dup_label=DUP_LABELS.get(picked.dup, ""),
        dup_note=DUP_NOTES.get(picked.dup, ""),
        dup_groups=groups[:DUP_GROUP_PREVIEW],
        dup_group_count=len(groups),
        # 여분은 묶음마다 한 건씩 남기고 센 수다. 지울 수 있는 최대치이지 지울 건수가 아니다
        dup_extra=total - len(groups),
        total=total,
        page=current,
        page_size=size,
        total_pages=total_pages,
        first_index=(current - 1) * size + 1 if rows else 0,
        last_index=(current - 1) * size + len(rows),
        page_numbers=_page_numbers(current, total_pages),
        page_url=lambda number: _page_url(criteria, number),
        # 지우기가 표와 같은 조건을 들고 가게 한다. 정렬과 페이지는 걸리는 행을 바꾸지 않아
        # 싣지 않는다
        delete_criteria=picked.as_form(),
        # 워크플로우를 골랐을 때만 그 사이트의 수집분을 통째로 비우는 길이 열린다
        workflow=workflow_label(conn, picked.workflow_id),
    )


@router.get("/ui/review/filters", response_class=HTMLResponse)
def review_filters_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    """조회 조건. 워크플로우와 회사는 지금 저장된 값에서 만든다.

    회사 목록은 두 칸을 합친 것이다. 자회사만 모으면 계열사를 말하지 않는 사이트가 목록에서
    통째로 사라지고, 모회사만 모으면 계열사를 고를 수 없다. 조건도 두 칸을 함께 본다
    (`app/api/review_filter.py` 의 `filter_sql`).
    """
    workflows = conn.execute("SELECT id, name FROM workflows ORDER BY id").fetchall()
    companies = conn.execute(
        """
        SELECT DISTINCT name FROM (
            SELECT parent_company_name AS name FROM normalized_jobs
             UNION
            SELECT company_name AS name FROM normalized_jobs
        )
         WHERE name IS NOT NULL AND TRIM(name) <> ''
         ORDER BY name
        """
    ).fetchall()
    return render(
        request,
        "fragments/review_filters.html",
        workflows=workflows,
        companies=[row["name"] for row in companies],
        # 켜진 대분류만 고를 수 있게 낸다(5.2). 꺼진 대분류로 이미 분류된 공고는 이 목록에
        # 없어도 조회 조건 값 자체는 그대로 받는다 — `read_filter` 가 표에 대지 않는다
        job_majors=[major.name for major in list_majors(conn, enabled_only=True)],
        page_sizes=PAGE_SIZES,
        default_page_size=DEFAULT_PAGE_SIZE,
        deadline_states=DEADLINE_STATES,
        delivery_states=DELIVERY_STATES,
        has_suggestion_states=HAS_SUGGESTION_STATES,
        empty_choices=EMPTY_CHOICES,
        empty_labels=EMPTY_LABELS,
        dup_criteria=DUP_CRITERIA,
        dup_labels=DUP_LABELS,
        dup_notes=DUP_NOTES,
        sort_labels=SORT_LABELS,
        default_sort=DEFAULT_SORT,
    )


@router.get("/ui/review/modal/{job_id}", response_class=HTMLResponse)
def review_modal_fragment(
    request: Request,
    job_id: int,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    """공고 한 건을 고치는 모달. 표 안에서 바로 고치는 경로는 두지 않는다.

    본문과 자격요건은 수백 자에 여러 줄인데, 표 칸 폭에 갇힌 입력에서는 고치는 값 전체가 한
    번에 보이지 않는다. 입구를 둘로 두면 어느 쪽이 저장된 값인지 화면에서 알 수 없어, 고치는
    자리를 이 모달 하나로 모은다.
    """
    return _modal_response(request, conn, job_id)


@router.put("/ui/review/jobs/{job_id}", response_class=HTMLResponse)
async def save_review_job_fragment(
    request: Request,
    job_id: int,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    """모달에서 고친 값을 한 번에 `job_field_overrides` 에 쌓는다.

    고친 필드만 쌓인다. 지금 화면에 있던 값을 그대로 돌려보낸 필드에는 보정을 만들지 않는다 —
    한 건을 열어 한 필드만 고쳤는데 여섯 개가 전부 사람 보정으로 굳으면, 다음 정규화에서
    규칙이 고쳐 놓을 값까지 옛 값에 붙들린다.

    `drop` 이 실려 오면 그 필드의 보정만 지우고 모달은 열어 둔다. 나머지 칸에 쳐 둔 값은
    그대로 돌려준다 — 보정 하나를 지우려다 아직 저장하지 않은 다른 수정을 잃지 않게 한다.

    `normalized_jobs` 에는 쓰지 않는다. 확정 값은 규칙과 보정에서 매번 다시 만들어지는
    파생값이고, 파생값에 손으로 쓰면 다음 재정규화가 그것을 덮어쓴다. `delivered_at` 도
    건드리지 않는다 — 수동 수정이 전달 표시를 되돌리면 소비 측에 같은 데이터가 다시 간다
    (`.claude/rules/data-safety.md`).
    """
    # 폼을 직접 읽는다. `Form()` 파라미터로 받으면 빈 칸이 기본값으로 바뀌어, 값을 지운
    # 필드와 아예 오지 않은 필드가 같아진다 — 그러면 틀린 값을 비우는 수정이 저장되지 않는다
    form = await request.form()
    submitted = {
        field: str(form[field]) for field in OVERRIDABLE_FIELDS if isinstance(form.get(field), str)
    }
    drop = str(form.get("drop") or "")
    # 보낸 값은 어느 경로든 그대로 돌려준다. 실패해도 방금 친 것이 입력에 남아야 한다
    drafts = {field: _text(value) for field, value in submitted.items()}

    job = _read_job(conn, job_id)
    if job is None:
        return _modal_response(request, conn, job_id)
    raw_job_id, part = _posting_key(job)

    if drop:
        if drop not in OVERRIDABLE_FIELDS:
            return _modal_response(
                request,
                conn,
                job_id,
                error=f"고칠 수 없는 필드다: {drop} (가능한 값: {', '.join(OVERRIDABLE_FIELDS)})",
                drafts=drafts,
            )
        conn.execute(
            "DELETE FROM job_field_overrides WHERE raw_job_id = ? AND part = ? AND field_name = ?",
            (raw_job_id, part, drop),
        )
        # 지운 필드는 규칙이 만든 값으로 돌아간다. 쳐 둔 값을 그대로 두면 화면만 옛 값이다
        drafts.pop(drop, None)
        return _modal_response(
            request,
            conn,
            job_id,
            saved=(
                f"{FIELD_LABELS[drop]} 보정을 지웠다. 다음 정규화에서 규칙이 만든 값으로 돌아간다"
            ),
            drafts=drafts,
            focus_field=drop,
            changed_fields=(drop,),
            swap_row=True,
        )

    overrides = _read_overrides(conn, [(raw_job_id, part)]).get((raw_job_id, part), {})
    changed: list[str] = []
    for field, value in submitted.items():
        new = _text(value)
        current = _text(overrides[field]) if field in overrides else _text(job[field])
        if new == current:
            continue
        try:
            _upsert_override(conn, raw_job_id, field, new, part)
        except sqlite3.DatabaseError as exc:
            # 실패 사유를 모달 안에 그대로 보여준다. 모달은 닫지 않고 고쳐 쓴 값도 입력에 남긴다
            return _modal_response(
                request,
                conn,
                job_id,
                error=f"{FIELD_LABELS[field]} 을 저장하지 못했다: {exc}",
                drafts=drafts,
                focus_field=field,
                changed_fields=tuple(changed),
                swap_row=bool(changed),
            )
        changed.append(field)

    if not changed:
        # 조용히 닫지 않는다. 닫히면 저장된 줄 알고, 아무 데도 남지 않은 수정을 찾게 된다
        return _modal_response(
            request,
            conn,
            job_id,
            note="고친 값이 없다. 보정은 만들지 않았다",
            drafts=drafts,
        )

    labels = ", ".join(FIELD_LABELS[field] for field in changed)
    return _modal_response(
        request,
        conn,
        job_id,
        saved=f"{len(changed)}개 필드 보정을 저장했다: {labels}",
        changed_fields=tuple(changed),
        swap_row=True,
        close=True,
    )


# 제안 처리 두 가지. `drop` 처럼 값이 정해진 파라미터로 두지 않는다. 어느 필드의 제안인지가
# 주소에 있으면 실수로 다른 필드의 제안을 지우는 요청을 만들 수 없다
SUGGESTION_ACCEPT = "accept"
SUGGESTION_REJECT = "reject"
SUGGESTION_ACTIONS: tuple[str, ...] = (SUGGESTION_ACCEPT, SUGGESTION_REJECT)


@router.post("/ui/review/suggestions/{job_id}/{field}", response_class=HTMLResponse)
async def apply_suggestion_fragment(
    request: Request,
    job_id: int,
    field: str,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    """그 칸의 제안을 수락하거나 거절한다. 모달 안의 필드 블록에서만 누른다 (11.6).

    수락은 제안 값을 `job_field_overrides` 에 넣는 것이지, `raw_jobs` 나 `normalized_jobs` 를
    고치는 것이 아니다 — 사람이 손으로 고친 값과 같은 자리다. 거절은 `job_field_suggestions`
    의 그 행만 지운다. 어느 쪽이든 처리한 제안은 이 표에서 사라져 다시 뜨지 않는다.

    모달은 닫지 않는다. 여러 필드의 제안을 오가며 판단하는 화면이라, 하나를 처리했다고
    나머지를 볼 기회를 잃으면 안 된다.
    """
    form = await request.form()
    action = str(form.get("action") or "")

    if field not in OVERRIDABLE_FIELDS:
        return _modal_response(
            request,
            conn,
            job_id,
            error=f"고칠 수 없는 필드다: {field} (가능한 값: {', '.join(OVERRIDABLE_FIELDS)})",
        )
    if action not in SUGGESTION_ACTIONS:
        return _modal_response(
            request,
            conn,
            job_id,
            error=f"알 수 없는 처리다: {action} (가능한 값: {', '.join(SUGGESTION_ACTIONS)})",
        )

    job = _read_job(conn, job_id)
    if job is None:
        return _modal_response(request, conn, job_id)
    raw_job_id, part = _posting_key(job)
    suggestion = read_suggestions(conn, raw_job_id, part).get(field)
    if suggestion is None:
        # 다른 창에서 이미 처리됐거나, 다시 분류가 돌며 사라졌을 수 있다. 표는 최신 상태로
        # 다시 그려 준다 — 방금 처리한 줄 알고 다시 누르는 것을 막는다
        return _modal_response(
            request,
            conn,
            job_id,
            note=f"{FIELD_LABELS[field]} 의 제안이 이미 처리됐다. 지금 상태로 다시 불러왔다",
            swap_row=True,
        )

    if action == SUGGESTION_ACCEPT:
        _upsert_override(conn, raw_job_id, field, suggestion["value"], part)
        conn.execute(
            "DELETE FROM job_field_suggestions"
            " WHERE raw_job_id = ? AND part = ? AND field_name = ?",
            (raw_job_id, part, field),
        )
        saved = f"{FIELD_LABELS[field]} 제안을 수락해 사람 보정으로 저장했다: {suggestion['value']}"
    else:
        conn.execute(
            "DELETE FROM job_field_suggestions"
            " WHERE raw_job_id = ? AND part = ? AND field_name = ?",
            (raw_job_id, part, field),
        )
        saved = f"{FIELD_LABELS[field]} 제안을 거절했다. 지금 값은 그대로다"

    return _modal_response(
        request,
        conn,
        job_id,
        saved=saved,
        focus_field=field,
        changed_fields=(field,),
        swap_row=True,
    )
