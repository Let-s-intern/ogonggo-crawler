"""부트캠프 화면의 조각 라우트 (2026-09-22 결정, LC-3364).

위 메뉴 `부트캠프` 한 페이지가 조각 하나(`fragments/bootcamp_panel.html`)로 돈다.
설정 저장, 지금 수집, 지금 보내기가 모두 이 조각을 다시 그려 돌려준다.
수집·전송은 `app/bootcamp/` 가 한다.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.api.settings import get_connection
from app.api.ui import render
from app.bootcamp import deliver, schedule, store
from app.bootcamp import settings as bootcamp_settings
from app.bootcamp.runner import MANUAL, run_sesac
from app.bootcamp.sesac import list_url
from app.config import Settings, get_settings
from app.deliver import settings as deliver_store
from app.scheduler import get_gate, get_scheduler

router = APIRouter(tags=["ui"], include_in_schema=False)

RECENT_RUNS = 5


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
        "fragments/bootcamp_panel.html",
        config=bootcamp_settings.read_config(conn),
        deliver_configured=deliver_store.read_config(conn).configured,
        key_configured=bool(settings.ogonggo_internal_api_key.strip()),
        next_run=_iso(schedule.next_run_time(get_scheduler().scheduler)),
        runs=conn.execute(
            "SELECT * FROM bootcamp_runs ORDER BY id DESC LIMIT ?", (RECENT_RUNS,)
        ).fetchall(),
        rows=rows,
        payloads={
            int(row["id"]): deliver.payload(row) for row in rows if row["filled_hash"] is not None
        },
        pending=deliver.pending_count(conn),
        max_attempts=deliver.MAX_ATTEMPTS,
        list_url=list_url(),
        message=message,
        error=error,
    )


def _iso(value: object | None) -> str | None:
    isoformat = getattr(value, "isoformat", None)
    return str(isoformat()) if callable(isoformat) else None


@router.get("/ui/bootcamps", response_class=HTMLResponse)
def bootcamp_panel_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    return _panel(request, conn, settings)


@router.put("/ui/bootcamps/settings", response_class=HTMLResponse)
def update_bootcamp_settings_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
    interval_hours: Annotated[int, Form()] = bootcamp_settings.DEFAULT_INTERVAL_HOURS,
    schedule_enabled: Annotated[str, Form()] = "",
    deliver_enabled: Annotated[str, Form()] = "",
) -> HTMLResponse:
    config = bootcamp_settings.BootcampConfig(
        schedule_enabled=schedule_enabled == "1",
        interval_hours=interval_hours,
        deliver_enabled=deliver_enabled == "1",
    )
    try:
        bootcamp_settings.write_config(conn, config)
    except bootcamp_settings.BootcampSettingError as exc:
        return _panel(request, conn, settings, error=str(exc))
    schedule.sync(get_scheduler().scheduler, conn)
    return _panel(request, conn, settings, message="저장했다")


@router.post("/ui/bootcamps/run", response_class=HTMLResponse)
async def run_bootcamps_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """지금 한 번 수집한다. 새 과정마다 AI 를 부르므로 몇 분 걸릴 수 있다."""
    async with get_gate().slot():
        summary = await run_sesac(conn, trigger=MANUAL, settings=settings)
    text = (
        f"목록 {summary.listed}건, 새 과정 {summary.new}건, 바뀐 과정 {summary.changed}건,"
        f" AI 정리 {summary.filled}건, 오공고 전송 {summary.sent}건, 실패 {summary.failed}건"
    )
    if summary.status == "failed":
        return _panel(request, conn, settings, error=f"수집이 실패했다. {text}")
    return _panel(request, conn, settings, message=text)


@router.post("/ui/bootcamps/send", response_class=HTMLResponse)
async def send_bootcamps_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """정리가 끝났는데 아직 보내지 않은 과정을 보낸다. 켜기·끄기와 시도 상한을 보지 않는다."""
    result = await deliver.deliver_pending(conn, settings=settings, retry_failed=True)
    if result.reason:
        return _panel(request, conn, settings, error=result.reason)
    if not result.sent and not result.failed:
        return _panel(request, conn, settings, message="보낼 과정이 없다")
    return _panel(
        request,
        conn,
        settings,
        message=f"오공고에 {result.sent}건 보냈다. 실패 {result.failed}건",
    )
