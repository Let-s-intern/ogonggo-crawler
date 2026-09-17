"""설정 > 수집 항목. 공고마다 채우는 칸을 보고, 운영자가 새 항목을 더한다 (2026-09-17, LC-3344).

위 표는 오공고로 보내는 칸이다 — 어디서 오는지(사이트·AI·자동), 오공고의 어느 칸인지, 지금 공고 중
몇 %가 찼는지. 코드와 오공고 서버가 함께 정하는 칸이라 여기서 더하거나 끄지 않는다.

아래 표는 운영자가 더한 항목이다 (`app/custom_fields.py`). 더하기 전에 최근 공고 3건으로 미리 보고,
더한 뒤에는 새로 분류하는 공고부터 채워진다. 이미 들어온 공고는 `다시 채우기` 로 최근 것부터 채운다.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app import custom_fields, db
from app.api import crawlers, job_detail
from app.api.review_filter import _filled
from app.api.ui import render
from app.classify.schema import FALLBACK_FIELDS
from app.llm.base import LlmCallError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ui"], include_in_schema=False)

# 미리 보기에 쓰는 최근 공고 수
PREVIEW_JOBS = 3
# 다시 채우기에서 고를 수 있는 건수
FILL_LIMITS: tuple[int, ...] = (20, 50, 100, 300)

SOURCE_WORDS: dict[str, str] = {
    job_detail.SOURCE_SITE: "사이트",
    job_detail.SOURCE_AI: "AI",
    job_detail.SOURCE_AUTO: "자동",
}


@dataclass
class FillProgress:
    """다시 채우기 한 번의 진행. 한 번에 하나만 돈다."""

    field_label: str = ""
    total: int = 0
    done: int = 0
    filled: int = 0
    running: bool = False
    error: str = ""
    tasks: list[asyncio.Task[None]] = field(default_factory=list)


_progress = FillProgress()


def _builtin_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """오공고로 보내는 칸과 채움률. 한 번의 질의로 센다."""
    fields = job_detail.FIELDS
    picks = ", ".join(
        f"SUM(CASE WHEN {_filled('n.' + item.name)} THEN 1 ELSE 0 END) AS f{index}"
        for index, item in enumerate(fields)
    )
    row = conn.execute(f"SELECT count(*) AS total, {picks} FROM normalized_jobs n").fetchone()
    total = int(row["total"] or 0)
    result: list[dict[str, Any]] = []
    for index, item in enumerate(fields):
        filled = int(row[f"f{index}"] or 0)
        result.append(
            {
                "label": item.label,
                "source": (
                    "사이트, 없으면 AI"
                    if item.name in FALLBACK_FIELDS
                    else SOURCE_WORDS[job_detail.source_of(item.name, {}, True)]
                ),
                "spring": job_detail.SPRING_NAMES.get(item.name, "—"),
                "pct": round(filled / total * 100) if total else 0,
            }
        )
    return result


def _list(request: Request, conn: sqlite3.Connection, message: str = "") -> HTMLResponse:
    filled, total = custom_fields.fill_counts(conn)
    customs = [
        (item, round(filled.get(item.id, 0) / total * 100) if total else 0)
        for item in custom_fields.list_fields(conn)
    ]
    return render(
        request,
        "fragments/fields_list.html",
        builtin=_builtin_rows(conn),
        customs=customs,
        message=message,
        progress=_progress,
        fill_limits=FILL_LIMITS,
    )


@router.get("/ui/fields", response_class=HTMLResponse)
def fields_fragment(
    request: Request, conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)]
) -> HTMLResponse:
    return _list(request, conn)


@router.get("/ui/fields/new", response_class=HTMLResponse)
def field_form(request: Request) -> HTMLResponse:
    """항목 추가 창."""
    return render(
        request,
        "fragments/field_form.html",
        types=custom_fields.TYPES,
        values={"label": "", "instruction": "", "value_type": "text", "choices": ""},
        error="",
    )


def _draft(
    label: str, instruction: str, value_type: str, choices: str
) -> custom_fields.CustomField:
    label, instruction, value_type, choices = custom_fields.validate(
        label, instruction, value_type, choices
    )
    return custom_fields.CustomField(
        id=0,
        label=label,
        instruction=instruction,
        value_type=value_type,
        choices=tuple(choices.splitlines()),
        enabled=True,
    )


@router.post("/ui/fields/preview", response_class=HTMLResponse)
async def field_preview(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
    label: Annotated[str, Form()] = "",
    instruction: Annotated[str, Form()] = "",
    value_type: Annotated[str, Form()] = "text",
    choices: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """저장하지 않고 최근 공고 몇 건에 물어본다. 결과는 창 안에만 보인다."""
    try:
        draft = _draft(label, instruction, value_type, choices)
    except custom_fields.CustomFieldError as exc:
        return render(request, "fragments/field_preview.html", error=str(exc), rows=[])
    jobs = conn.execute(
        "SELECT title, body, source_url FROM normalized_jobs"
        " WHERE length(trim(coalesce(body, ''))) > 0 ORDER BY id DESC LIMIT ?",
        (PREVIEW_JOBS,),
    ).fetchall()
    rows: list[tuple[str, str]] = []
    for job in jobs:
        try:
            values = await custom_fields.extract(
                conn, [draft], str(job["title"] or ""), str(job["body"] or "")
            )
        except LlmCallError as exc:
            return render(
                request,
                "fragments/field_preview.html",
                error=f"AI 에게 묻지 못했다: {exc}",
                rows=rows,
            )
        rows.append((str(job["title"] or "제목 없음"), values.get(0, "")))
    return render(request, "fragments/field_preview.html", error="", rows=rows)


@router.post("/ui/fields", response_class=HTMLResponse)
def field_add(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
    label: Annotated[str, Form()] = "",
    instruction: Annotated[str, Form()] = "",
    value_type: Annotated[str, Form()] = "text",
    choices: Annotated[str, Form()] = "",
) -> HTMLResponse:
    try:
        added = custom_fields.add_field(conn, label, instruction, value_type, choices)
    except custom_fields.CustomFieldError as exc:
        return render(
            request,
            "fragments/field_form.html",
            types=custom_fields.TYPES,
            values={
                "label": label,
                "instruction": instruction,
                "value_type": value_type,
                "choices": choices,
            },
            error=str(exc),
        )
    response = render(request, "fragments/field_added.html", field=added)
    response.headers["HX-Trigger"] = "fields-changed"
    return response


@router.post("/ui/fields/{field_id}/toggle", response_class=HTMLResponse)
def field_toggle(
    request: Request,
    field_id: int,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    found = custom_fields.read_field(conn, field_id)
    if found is None:
        return _list(request, conn, "그 항목이 없다")
    custom_fields.set_enabled(conn, field_id, not found.enabled)
    word = "껐다. 새로 분류하는 공고에서 더 채우지 않는다" if found.enabled else "켰다"
    return _list(request, conn, f"{found.label} 항목을 {word}")


@router.post("/ui/fields/{field_id}/delete", response_class=HTMLResponse)
def field_delete(
    request: Request,
    field_id: int,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    found = custom_fields.read_field(conn, field_id)
    if found is None:
        return _list(request, conn, "그 항목이 없다")
    custom_fields.delete_field(conn, field_id)
    return _list(request, conn, f"{found.label} 항목과 채운 값을 지웠다")


def get_fill_launcher() -> Any:
    """다시 채우기를 요청 밖에서 돌린다. 테스트는 이 의존성을 갈아끼운다."""

    def launch(coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        _progress.tasks = [task]

    return launch


def get_fill_connect() -> Any:
    return db.connect


async def _fill(field_id: int, limit: int, connect: Any) -> None:
    conn = connect()
    try:
        found = custom_fields.read_field(conn, field_id)
        if found is None:
            return
        ids = [
            int(row["raw_job_id"])
            for row in conn.execute(
                "SELECT DISTINCT raw_job_id FROM normalized_jobs ORDER BY raw_job_id DESC LIMIT ?",
                (limit,),
            )
        ]
        _progress.total = len(ids)
        for raw_job_id in ids:
            _progress.filled += await custom_fields.fill_job(conn, raw_job_id, fields=[found])
            _progress.done += 1
    except Exception as exc:
        logger.exception("추가 항목 다시 채우기가 예외로 끝났다")
        _progress.error = f"{type(exc).__name__}: {exc}"
    finally:
        _progress.running = False
        conn.close()


@router.post("/ui/fields/{field_id}/fill", response_class=HTMLResponse)
async def field_fill(
    request: Request,
    field_id: int,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
    launch: Annotated[Any, Depends(get_fill_launcher)],
    connect: Annotated[Any, Depends(get_fill_connect)],
    limit: Annotated[int, Form()] = FILL_LIMITS[0],
) -> HTMLResponse:
    """최근 공고부터 `limit` 건을 이 항목으로 다시 채운다. 시작만 하고 진행은 목록이 물어본다."""
    found = custom_fields.read_field(conn, field_id)
    if found is None:
        return _list(request, conn, "그 항목이 없다")
    if _progress.running:
        return _list(request, conn, "다른 항목을 채우는 중이다. 끝난 뒤 다시 누른다")
    limit = max(1, min(limit, FILL_LIMITS[-1]))
    _progress.field_label = found.label
    _progress.total = limit
    _progress.done = 0
    _progress.filled = 0
    _progress.error = ""
    _progress.running = True
    launch(_fill(field_id, limit, connect))
    return _list(request, conn, f"{found.label} 항목을 최근 공고 {limit}건에 채우기 시작했다")
