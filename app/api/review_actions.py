"""공고 목록에서 공고를 고치고, AI 로 다시 채우고, 오공고로 보낸다 (2026-09-17 결정, LC-3344).

패널 보기는 `app/api/review.py` 다. 여기 있는 동작은 모두 끝나면 같은 패널을 다시 그린다 — 무엇이
바뀌었는지 운영자가 그 자리에서 본다.

## 고치기는 사람 보정으로 남긴다

값을 `normalized_jobs` 에 바로 쓰지 않는다. `job_field_overrides` 에 적고 그 공고를 다시
정규화한다. 정규화 순서가 규칙 → 분류 → 사람 보정이라(`app/normalize/engine.py`), 이렇게 해야
나중에 AI 로 다시 채워도 사람이 고친 칸이 남는다. 지금 값과 같은 칸은 적지 않는다 — 누르지 않은
칸까지 보정으로 굳으면 AI 가 더 나은 값을 내도 반영되지 않는다.

이미 오공고로 보낸 공고도 고칠 수는 있지만 오공고에는 다시 가지 않는다. 오공고에 고치는 경로가
없다 (`app/deliver/spring.py`).
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.api import crawlers, job_detail
from app.api.review import render_panel
from app.api.ui import render
from app.classify.batch import ClassifyProgress, classify_ids, get_classify_run
from app.deliver import spring
from app.normalize.backfill import rewrite_one
from app.normalize.engine import NormalizeError, load_rules
from app.side import runner as side_runner

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ui"], include_in_schema=False)


def _job(conn: sqlite3.Connection, normalized_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT n.*, EXISTS (SELECT 1 FROM spring_deliveries d WHERE d.source_url = n.source_url"
        " AND d.status = 'sent') AS sent FROM normalized_jobs n WHERE n.id = ?",
        (normalized_id,),
    ).fetchone()


def _sent_words(result: spring.DeliveryResult) -> str:
    """전송 결과 한 줄."""
    if result.reason:
        return f"오공고로 보내지 못했다: {result.reason}"
    if result.sent:
        return "오공고로 보냈다"
    if result.failed:
        return "오공고가 거절했다: " + "; ".join(
            error.split(": ", 1)[-1] for error in result.errors
        )
    return "보낼 공고가 없다. 이미 보낸 공고다"


@router.get("/ui/review/jobs/{normalized_id}/edit", response_class=HTMLResponse)
def job_edit_form(
    request: Request,
    normalized_id: int,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    """패널을 고치기 모드로. 칸이 입력이 된다."""
    return render_panel(request, conn, normalized_id, editing=True)


@router.post("/ui/review/jobs/{normalized_id}/edit", response_class=HTMLResponse)
async def job_edit_submit(
    request: Request,
    normalized_id: int,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    """고친 칸을 사람 보정으로 적고 그 공고를 다시 정규화한다. `send` 면 이어서 보낸다."""
    job = _job(conn, normalized_id)
    if job is None:
        return render_panel(request, conn, normalized_id)
    form = await request.form()
    raw_job_id, part = int(job["raw_job_id"]), int(job["part"])

    # 직군·직무·산업은 설정의 목록 안에서만 받는다. 직무는 고친 뒤의 직군 아래에 있어야 한다
    def picked(name: str) -> str:
        return str(form[name]).strip() if name in form else str(job[name] or "").strip()

    wrong = job_detail.list_choices(conn).check(
        picked("job_field"), picked("job_role"), picked("industry")
    )
    if wrong:
        return render_panel(request, conn, normalized_id, editing=True, message=wrong)

    changed: list[str] = []
    for field in job_detail.EDITABLE_FIELDS:
        if field.name not in form:
            continue
        value = str(form[field.name]).strip()
        if value == str(job[field.name] or "").strip():
            continue
        if field.kind == job_detail.KIND_CHOICE and value and value not in field.choices:
            return render_panel(
                request,
                conn,
                normalized_id,
                editing=True,
                message=f"{field.label} 에 목록 밖 값이 왔다. 화면을 다시 불러 고른다",
            )
        conn.execute(
            """
            INSERT INTO job_field_overrides (raw_job_id, part, field_name, value)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (raw_job_id, part, field_name)
            DO UPDATE SET value = excluded.value, updated_at = datetime('now')
            """,
            (raw_job_id, part, field.name, value),
        )
        changed.append(field.label)

    if changed:
        try:
            rewrite_one(conn, raw_job_id, load_rules(conn))
        except NormalizeError as exc:
            return render_panel(
                request,
                conn,
                normalized_id,
                message=f"고친 값은 적었지만 다시 정규화하지 못했다: {exc}",
            )
        logger.info("공고 %s 를 고쳤다: %s", normalized_id, ", ".join(changed))
        message = f"{len(changed)}칸을 고쳤다: {', '.join(changed)}"
        if job["sent"]:
            message += ". 이미 보낸 공고라 오공고에는 반영되지 않는다"
    else:
        message = "바뀐 칸이 없다"

    if form.get("send") and not job["sent"]:
        result = await spring.deliver_ids(conn, [normalized_id])
        message = f"{message}. {_sent_words(result)}"
    return render_panel(request, conn, normalized_id, message=message)


@router.post("/ui/review/jobs/{normalized_id}/send", response_class=HTMLResponse)
async def job_send(
    request: Request,
    normalized_id: int,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    """이 공고 하나를 지금 오공고로 보낸다."""
    result = await spring.deliver_ids(conn, [normalized_id])
    return render_panel(request, conn, normalized_id, message=_sent_words(result))


@router.post("/ui/review/jobs/{normalized_id}/reclassify", response_class=HTMLResponse)
async def job_reclassify(
    request: Request,
    normalized_id: int,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    """본문을 AI 로 다시 읽어 칸을 채운다. 사람이 고친 칸은 정규화 순서상 그대로 남는다."""
    job = _job(conn, normalized_id)
    if job is None:
        return render_panel(request, conn, normalized_id)
    busy = side_runner.classify_running(conn) or (
        "분류 실행이 아직 돌고 있다" if get_classify_run().progress().running else None
    )
    if busy:
        return render_panel(
            request, conn, normalized_id, message=f"지금은 다시 채울 수 없다: {busy}"
        )
    before = dict(job)
    progress = await classify_ids(conn, [int(job["raw_job_id"])], ClassifyProgress())
    if not progress.processed:
        message = "AI로 다시 채우지 못했어요: " + ("; ".join(progress.errors) or "사유 없음")
        return render_panel(request, conn, normalized_id, message=message)
    after = _job(conn, normalized_id)
    # 무엇이 바뀌었는지 적는다. "다시 채웠다" 만으로는 누른 보람이 있었는지 알 수 없다
    changed = [
        field.label
        for field in job_detail.FIELDS
        if after is not None and (before.get(field.name) or "") != (after[field.name] or "")
    ]
    message = (
        f"AI로 다시 채웠어요. 바뀐 칸: {', '.join(changed)}"
        if changed
        else "AI로 다시 읽었지만 바뀐 칸은 없어요"
    )
    return render_panel(request, conn, normalized_id, message=message)


@router.post("/ui/review/send", response_class=HTMLResponse)
async def jobs_send(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
    raw_job_id: Annotated[list[int] | None, Form()] = None,
) -> HTMLResponse:
    """표에서 고른 공고를 한꺼번에 보낸다. 결과는 확인 창에 적는다.

    표의 체크박스는 수집 건 번호다. 한 수집 건이 여러 공고로 나뉘었으면 그 공고 전부를 보낸다.
    """
    wanted = sorted(set(raw_job_id or []))
    ids: list[int] = []
    for start in range(0, len(wanted), 500):
        part = wanted[start : start + 500]
        marks = ",".join("?" for _ in part)
        ids.extend(
            int(row["id"])
            for row in conn.execute(
                f"SELECT id FROM normalized_jobs WHERE raw_job_id IN ({marks})", part
            )
        )
    result = await spring.deliver_ids(conn, ids)
    response = render(
        request,
        "fragments/review_send_result.html",
        picked=len(ids),
        result=result,
    )
    # 표와 숫자 카드를 다시 부르게 한다. 지우기와 같은 이벤트다
    response.headers["HX-Trigger"] = "jobs-deleted"
    return response
