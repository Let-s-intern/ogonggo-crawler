"""공고 분석 화면의 조각 라우트 (2026-10-08 결정, LC-3446).

위 메뉴 `공고 분석` 한 페이지다. 설정 > AI 공고 분석이 "어떻게 분석할지" 를 고치는 곳이라면, 여기는
"무엇을 분석했고 오공고로 어디까지 보냈는지" 를 보는 곳이다. 매일 분석 설정, 지금 분석, 최근 실행,
공고 목록이 조각 하나(`fragments/job_analyses_panel.html`)로 돌고, 행을 누르면 오른쪽 패널에 그
공고의 분석을 오공고 화면 모양으로 연다. 분석·전송은 `app/job_analysis/runner.py` 가 한다.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse

from app import db
from app.api.settings import get_connection
from app.api.ui import render
from app.config import Settings, get_settings
from app.deliver import settings as deliver_store
from app.job_analysis import guide as guides
from app.job_analysis import ogonggo, runner, schedule, store
from app.job_analysis import settings as analysis_settings
from app.job_analysis.analyzer import JOB_ANALYSIS, AnalysisError, analyze
from app.job_analysis.posting import Posting
from app.job_analysis.schema import EMPLOYMENT_KEYS, SUBMISSION_KEYS
from app.llm.pricing import cost_usd
from app.scheduler import get_scheduler

router = APIRouter(tags=["ui"], include_in_schema=False)

RECENT_RUNS = 5


def get_analysis_client() -> Any | None:
    """다시 분석이 쓸 모델 클라이언트. None 이면 설정으로 만든다. 테스트가 가짜로 바꾸는 자리다."""
    return None


def _midnight_utc(settings: Settings) -> str:
    """표시 시간대의 오늘 자정을 DB 가 적는 UTC 글자로."""
    zone = ZoneInfo(settings.display_timezone)
    midnight = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _today_cost(conn: sqlite3.Connection, since: str) -> float | None:
    rows = conn.execute(
        "SELECT model, sum(input_tokens) AS i, sum(output_tokens) AS o FROM llm_calls"
        " WHERE feature = ? AND called_at >= ? GROUP BY model",
        (JOB_ANALYSIS, since),
    ).fetchall()
    costs = [cost_usd(str(row["model"]), int(row["i"] or 0), int(row["o"] or 0)) for row in rows]
    known = [cost for cost in costs if cost is not None]
    if rows and not known:
        return None
    return sum(known)


def _panel(
    request: Request,
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    status: str = "",
    source: str = "",
    query: str = "",
    page: int = 1,
    message: str = "",
    error: str = "",
) -> HTMLResponse:
    since = _midnight_utc(settings)
    rows, total = store.listing(conn, status=status, source=source, query=query, page=page)
    try:
        version = guides.current(conn).number
    except guides.GuideError:
        version = None
    return render(
        request,
        "fragments/job_analyses_panel.html",
        config=analysis_settings.read_config(conn),
        deliver_configured=deliver_store.read_config(conn).configured,
        key_configured=bool(settings.ogonggo_internal_api_key.strip()),
        next_run=_iso(schedule.next_run_time(get_scheduler().scheduler)),
        runs=conn.execute(
            "SELECT * FROM job_analysis_runs ORDER BY id DESC LIMIT ?", (RECENT_RUNS,)
        ).fetchall(),
        today=store.today(conn, since),
        today_cost=_today_cost(conn, since),
        counts=store.counts(conn),
        rows=rows,
        competencies={int(row["id"]): _competencies(row) for row in rows},
        total=total,
        page=page,
        pages=max(1, -(-total // store.PAGE_SIZE)),
        filters={"status": status, "source": source, "q": query},
        statuses=store.STATUSES,
        status_labels=store.STATUS_LABELS,
        sources=ogonggo.SOURCE_LABELS,
        version=version,
        running=_busy(conn),
        max_per_run=runner.MAX_PER_RUN,
        message=message,
        error=error,
    )


def _competencies(row: sqlite3.Row) -> int | None:
    analysis = store.analysis_of(row)
    return len(analysis.get("competencies") or []) if analysis is not None else None


def _iso(value: object | None) -> str | None:
    isoformat = getattr(value, "isoformat", None)
    return str(isoformat()) if callable(isoformat) else None


@router.get("/ui/job-analyses", response_class=HTMLResponse)
def job_analyses_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
    status: Annotated[str, Query()] = "",
    source: Annotated[str, Query()] = "",
    q: Annotated[str, Query()] = "",
    page: Annotated[int, Query(ge=1)] = 1,
) -> HTMLResponse:
    return _panel(request, conn, settings, status=status, source=source, query=q.strip(), page=page)


@router.put("/ui/job-analyses/settings", response_class=HTMLResponse)
def update_job_analyses_settings_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
    start_time: Annotated[str, Form()] = analysis_settings.DEFAULT_START_TIME,
    stop_time: Annotated[str, Form()] = analysis_settings.DEFAULT_STOP_TIME,
    schedule_enabled: Annotated[str, Form()] = "",
) -> HTMLResponse:
    config = analysis_settings.JobAnalysisConfig(
        schedule_enabled=schedule_enabled == "1",
        start_time=start_time.strip(),
        stop_time=stop_time.strip(),
    )
    try:
        analysis_settings.write_config(conn, config)
    except analysis_settings.JobAnalysisSettingError as exc:
        return _panel(request, conn, settings, error=str(exc))
    schedule.sync(get_scheduler().scheduler, conn)
    return _panel(request, conn, settings, message="저장했어요")


@router.post("/ui/job-analyses/run", response_class=HTMLResponse)
async def run_job_analyses_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """지금 한 번 분석한다. 백그라운드에서 돌고 화면은 끝날 때까지 몇 초마다 다시 그린다.

    지금 분석은 멈춤 시각을 보지 않는다 — 사람이 누른 것이라 상한까지 간다.
    """
    if _busy(conn):
        return _panel(request, conn, settings, message="이미 분석하는 중이에요")
    _start(settings)
    return _panel(
        request, conn, settings, message="분석을 시작했어요. 끝나면 최근 실행에 결과가 나와요"
    )


_task: asyncio.Task[None] | None = None


def _start(settings: Settings) -> None:
    global _task

    async def run() -> None:
        background = db.connect()
        try:
            await runner.run_once(background, trigger=runner.MANUAL, settings=settings)
        finally:
            background.close()

    _task = asyncio.create_task(run())


def _busy(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM job_analysis_runs WHERE status = 'running' LIMIT 1"
    ).fetchone()
    return row is not None or (_task is not None and not _task.done())


def _record(
    request: Request,
    conn: sqlite3.Connection,
    record_id: int,
    *,
    message: str = "",
    error: str = "",
) -> HTMLResponse:
    row = store.get_by_id(conn, record_id)
    posting = Posting.of(store.posting_of(row)) if row is not None else None
    return render(
        request,
        "fragments/job_analysis_record.html",
        row=row,
        data=store.analysis_of(row) if row is not None else None,
        posting=posting,
        status_labels=store.STATUS_LABELS,
        sources=ogonggo.SOURCE_LABELS,
        employment_keys=EMPLOYMENT_KEYS,
        submission_keys=SUBMISSION_KEYS,
        cost=(
            cost_usd(str(row["model"]), int(row["input_tokens"]), int(row["output_tokens"]))
            if row is not None
            else None
        ),
        message=message,
        error=error,
    )


@router.get("/ui/job-analyses/{record_id}", response_class=HTMLResponse)
def job_analysis_record_fragment(
    request: Request,
    record_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> HTMLResponse:
    return _record(request, conn, record_id)


@router.post("/ui/job-analyses/{record_id}/resend", response_class=HTMLResponse)
async def resend_job_analysis_fragment(
    request: Request,
    record_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """남은 분석을 다시 보낸다. AI 를 부르지 않는다."""
    row = store.get_by_id(conn, record_id)
    analyzed = runner.analyzed_of(row) if row is not None else None
    if row is None or analyzed is None:
        return _record(request, conn, record_id, error="보낼 분석이 없어요. 먼저 다시 분석해요")
    try:
        async with ogonggo.connection(conn, settings) as client:
            sent = await runner.send(
                conn, client, int(row["ogonggo_job_id"]), str(row["content_hash"]), analyzed
            )
    except ogonggo.OgonggoUnavailable as exc:
        return _record(request, conn, record_id, error=str(exc))
    if sent:
        return _record(request, conn, record_id, message="오공고로 보냈어요")
    return _record(request, conn, record_id, error="보내지 못했어요. 아래 사유를 확인해요")


@router.post("/ui/job-analyses/{record_id}/reanalyze", response_class=HTMLResponse)
async def reanalyze_job_analysis_fragment(
    request: Request,
    record_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
    client: Annotated[Any | None, Depends(get_analysis_client)],
) -> HTMLResponse:
    """남은 공고 글을 지금 판으로 다시 분석하고 오공고로 보낸다."""
    row = store.get_by_id(conn, record_id)
    if row is None:
        return _record(request, conn, record_id)
    job_id = int(row["ogonggo_job_id"])
    try:
        version = guides.current(conn)
        result = await analyze(
            conn,
            Posting.of(store.posting_of(row)),
            version.guide,
            settings=settings,
            client=client,
            guide_version=version.number,
        )
    except (AnalysisError, guides.GuideError) as exc:
        store.mark(conn, job_id, store.FAILED, str(exc), counted=True)
        return _record(request, conn, record_id, error=f"분석하지 못했어요: {exc}")
    analyzed = store.Analyzed(
        result.analysis, version.number, result.model, result.input_tokens, result.output_tokens
    )
    store.save_analysis(conn, job_id, analyzed, None)
    try:
        async with ogonggo.connection(conn, settings) as http:
            sent = await runner.send(conn, http, job_id, str(row["content_hash"]), analyzed)
    except ogonggo.OgonggoUnavailable as exc:
        store.mark(conn, job_id, store.UNSENT, str(exc))
        return _record(request, conn, record_id, error=f"분석은 했지만 보내지 못했어요: {exc}")
    if sent:
        return _record(
            request, conn, record_id, message=f"판 {version.number} 으로 다시 분석해 보냈어요"
        )
    return _record(request, conn, record_id, error="분석은 했지만 보내지 못했어요")
