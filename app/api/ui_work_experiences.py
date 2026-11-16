"""미래내일 일경험 화면의 조각 라우트 (2026-10-02 결정 LC-3432, CSV 올리기 2026-11-16 LC-3605).

위 메뉴 `미래내일 일경험` 한 페이지가 조각 하나(`fragments/work_experience_panel.html`)로 돈다.
설정 저장, CSV 올리기, 정리 이어 하기, 지금 보내기가 모두 이 조각을 다시 그려 돌려준다. 정리·전송은
`app/work_experience/` 가 한다.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse

from app import db
from app.api.settings import get_connection
from app.api.ui import render
from app.config import Settings, get_settings
from app.deliver import settings as deliver_store
from app.scheduler import get_gate
from app.work_experience import deliver, source, store
from app.work_experience import settings as work_settings
from app.work_experience.runner import MAX_FILLS_PER_RUN, Programs, run_all

router = APIRouter(tags=["ui"], include_in_schema=False)

RECENT_RUNS = 5
# 공공데이터 CSV 는 440줄에 1MB 남짓이다. 넉넉히 받되 한없이 받지 않는다
MAX_BYTES = 20 * 1024 * 1024


def _panel(
    request: Request,
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    message: str = "",
    error: str = "",
) -> HTMLResponse:
    rows = store.listing(conn)
    return render(
        request,
        "fragments/work_experience_panel.html",
        config=work_settings.read_config(conn),
        deliver_configured=deliver_store.read_config(conn).configured,
        key_configured=bool(settings.ogonggo_internal_api_key.strip()),
        runs=conn.execute(
            "SELECT * FROM work_experience_runs ORDER BY id DESC LIMIT ?", (RECENT_RUNS,)
        ).fetchall(),
        rows=rows,
        payloads={
            int(row["id"]): deliver.payload(conn, row)
            for row in rows
            if row["filled_hash"] is not None
        },
        today=deliver.today(settings),
        pending=deliver.pending_count(conn, settings),
        running=_busy(conn),
        max_attempts=deliver.MAX_ATTEMPTS,
        max_fills=MAX_FILLS_PER_RUN,
        list_url=source.LIST_URL,
        message=message,
        error=error,
    )


@router.get("/ui/work-experiences", response_class=HTMLResponse)
def work_experience_panel_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    return _panel(request, conn, settings)


@router.put("/ui/work-experiences/settings", response_class=HTMLResponse)
def update_work_experience_settings_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
    deliver_enabled: Annotated[str, Form()] = "",
) -> HTMLResponse:
    work_settings.write_config(
        conn, work_settings.WorkExperienceConfig(deliver_enabled=deliver_enabled == "1")
    )
    return _panel(request, conn, settings, message="저장했다")


@router.post("/ui/work-experiences/upload", response_class=HTMLResponse)
async def upload_work_experiences_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
    sheet: Annotated[UploadFile | None, File()] = None,
) -> HTMLResponse:
    """프로그램번호·공유주소를 붙인 CSV 를 받아 넣고, AI 정리·전송을 백그라운드로 시작한다.

    새 프로그램마다 AI 를 부르므로 수십 분 걸린다. 요청 하나가 그동안 붙잡혀 있으면 프록시가 끊는다.
    """
    if _busy(conn):
        return _panel(request, conn, settings, message="이미 정리하는 중이다. 끝나고 올린다")
    if sheet is None or not sheet.filename:
        return _panel(request, conn, settings, error="올릴 CSV 파일을 골라 주세요")
    data = await sheet.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        return _panel(request, conn, settings, error="파일이 20MB 를 넘는다")
    try:
        parsed = source.parse_csv(data)
    except source.SourceError as exc:
        return _panel(request, conn, settings, error=str(exc))
    if not parsed.programs:
        return _panel(
            request,
            conn,
            settings,
            error=f"넣을 줄이 없다. {parsed.rows}줄 모두 프로그램번호나 모집종료일이 없다",
        )
    _start(settings, parsed.programs, parsed.skipped)
    skipped = f", 건너뛴 줄 {len(parsed.skipped)}개" if parsed.skipped else ""
    return _panel(
        request,
        conn,
        settings,
        message=f"CSV {len(parsed.programs)}건을 올렸다{skipped}. 정리가 끝나면 최근 기록에 나온다",
    )


@router.post("/ui/work-experiences/run", response_class=HTMLResponse)
async def run_work_experiences_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """정리하지 못한 프로그램(실패·상한에 걸려 남은 것)을 이어서 정리한다. CSV 는 받지 않는다."""
    if _busy(conn):
        return _panel(request, conn, settings, message="이미 정리하는 중이다")
    _start(settings, (), ())
    return _panel(
        request, conn, settings, message="정리를 시작했다. 끝나면 최근 기록에 결과가 나온다"
    )


_task: asyncio.Task[None] | None = None


def _start(settings: Settings, programs: Programs, skipped: Sequence[str]) -> None:
    global _task

    async def run() -> None:
        background = db.connect()
        try:
            await run_all(
                background,
                programs=programs,
                skipped_lines=skipped,
                settings=settings,
                slot=get_gate().slot,
            )
        finally:
            background.close()

    _task = asyncio.create_task(run())


def _busy(conn: sqlite3.Connection) -> bool:
    """정리 중인가. 묶음과 묶음 사이에는 `running` 기록이 잠깐 없어 백그라운드 작업도 본다."""
    return _running(conn) or (_task is not None and not _task.done())


def _running(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM work_experience_runs WHERE status = 'running' LIMIT 1"
    ).fetchone()
    return row is not None


@router.post("/ui/work-experiences/send", response_class=HTMLResponse)
async def send_work_experiences_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """정리가 끝났는데 아직 보내지 않은 프로그램을 보낸다. 켜기·끄기와 시도 상한을 보지 않는다."""
    result = await deliver.deliver_pending(conn, settings=settings, retry_failed=True)
    if result.reason:
        return _panel(request, conn, settings, error=result.reason)
    if not result.sent and not result.failed:
        return _panel(request, conn, settings, message="보낼 프로그램이 없다")
    return _panel(
        request,
        conn,
        settings,
        message=f"오공고에 {result.sent}건 보냈다. 실패 {result.failed}건",
    )
