"""사이트 전체 원문 다시 수집 (2026-10-02 결정).

사이트가 백 곳이 넘는데 원문 다시 수집은 사이트 카드마다 눌러야 했다. 마감일 읽기를 고친 뒤 이미
담은 공고 전부를 새 코드로 다시 읽어야 했고, 그것을 백 번 누를 수는 없다. 사이트 목록 위 단추
하나로 공고를 담은 사이트를 차례로 한 곳씩 돈다.

- **한 곳씩 돈다.** 사이트마다 원문 다시 수집이 끝나고(다시 분류까지) 다음으로 간다. 다시 분류는
  분류 자리를 하나만 쓰므로 여러 곳을 겹쳐 돌려도 빨라지지 않는다.
- **이미 돌고 있는 사이트는 건너뛴다.** 주기 수집이나 카드에서 누른 다시 수집과 겹치지 않는다.
- **멈추면 지금 사이트까지 하고 멈춘다.** 사이트 하나의 다시 수집은 중간에 끊지 않는다.
- 진행 상황은 프로세스 메모리에 있다. 재시작하면 사라지고, 끝난 사이트의 결과는 각 사이트의
  실행 기록(`crawl_runs.trigger = 'recollect'`)에 남는다.

한 사이트의 다시 수집이 하는 일은 `app/crawler/recollect.py` 다.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.api import crawlers, workflows
from app.api.ui import render
from app.api.ui_workflows import (
    Connect,
    Launcher,
    _in_flight,
    _running,
    get_run_connect,
    get_run_gate,
    get_run_launcher,
)
from app.crawler import recollect
from app.crawler.failures import SUCCESS
from app.crawler.fetcher import FetchPolicy
from app.scheduler import RunGate, WorkflowScheduler

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ui"], include_in_schema=False)


@dataclass
class AllProgress:
    """전체 다시 수집 한 번의 진행. 화면이 읽는 복사본을 `snapshot` 으로 준다."""

    running: bool = False
    include_closed: bool = True
    total: int = 0
    done: int = 0
    current: str = ""
    stop_requested: bool = False
    started_at: str = ""
    finished_at: str = ""
    # (사이트 이름, 사유)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)

    def snapshot(self) -> AllProgress:
        return replace(self, failed=list(self.failed), skipped=list(self.skipped))


_progress = AllProgress()


def progress() -> AllProgress:
    return _progress.snapshot()


def _targets(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """공고를 담은 사이트. 멈춘 사이트도 넣는다 — 이미 담은 공고의 값을 바로잡는 일이다."""
    rows = conn.execute(
        """
        SELECT w.id, w.name FROM workflows w
         WHERE EXISTS (SELECT 1 FROM raw_jobs r WHERE r.workflow_id = w.id)
         ORDER BY w.id
        """
    ).fetchall()
    return [(int(row["id"]), str(row["name"])) for row in rows]


def _job_count(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT count(*) AS n FROM raw_jobs").fetchone()["n"])


def _panel(request: Request, conn: sqlite3.Connection, message: str = "") -> HTMLResponse:
    return render(
        request,
        "fragments/recollect_all.html",
        progress=progress(),
        sites=len(_targets(conn)),
        jobs=_job_count(conn),
        message=message,
    )


@router.get("/ui/sites/recollect-all", response_class=HTMLResponse)
def recollect_all_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(workflows.get_connection)],
) -> HTMLResponse:
    """단추나 진행 상황. 도는 동안 이 조각이 몇 초마다 스스로 다시 불린다."""
    return _panel(request, conn)


@router.post("/ui/sites/recollect-all", response_class=HTMLResponse)
async def recollect_all_start(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(workflows.get_connection)],
    scheduler: Annotated[WorkflowScheduler, Depends(workflows.get_workflow_scheduler)],
    fetcher: Annotated[FetchPolicy, Depends(crawlers.get_crawl_fetcher)],
    gate: Annotated[RunGate, Depends(get_run_gate)],
    launch: Annotated[Launcher, Depends(get_run_launcher)],
    connect: Annotated[Connect, Depends(get_run_connect)],
    include_closed: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """공고를 담은 사이트를 차례로 다시 수집한다. 시작만 한다.

    `async` 여야 한다. 백그라운드 작업을 이 요청의 이벤트 루프에 올린다 (`ui_workflows._launch`).
    """
    global _progress
    if _progress.running:
        return _panel(request, conn, "이미 돌고 있어요")
    targets = _targets(conn)
    if not targets:
        return _panel(request, conn, "공고를 담은 사이트가 없어요")
    _progress = AllProgress(
        running=True,
        include_closed=bool(include_closed),
        total=len(targets),
        started_at=_now(),
    )
    logger.info("전체 원문 다시 수집을 시작한다: 사이트 %s곳", len(targets))
    launch(
        _run_all(
            targets,
            include_closed=bool(include_closed),
            fetcher=fetcher,
            scheduler=scheduler,
            gate=gate,
            connect=connect,
        )
    )
    return _panel(request, conn)


@router.post("/ui/sites/recollect-all/stop", response_class=HTMLResponse)
def recollect_all_stop(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(workflows.get_connection)],
) -> HTMLResponse:
    """지금 사이트까지 하고 멈춘다."""
    if _progress.running:
        _progress.stop_requested = True
    return _panel(request, conn)


async def _run_all(
    targets: list[tuple[int, str]],
    *,
    include_closed: bool,
    fetcher: FetchPolicy,
    scheduler: WorkflowScheduler,
    gate: RunGate,
    connect: Connect,
) -> None:
    state = _progress
    try:
        for workflow_id, name in targets:
            if state.stop_requested:
                break
            state.current = name
            conn = connect()
            try:
                if workflow_id in _running or _in_flight(conn, workflow_id):
                    state.skipped.append((name, "이미 수집이 돌고 있었다"))
                    continue
                _running.add(workflow_id)
                try:
                    result = await recollect.recollect_workflow(
                        conn,
                        workflow_id,
                        fetcher=fetcher,
                        slot=gate.slot,
                        include_closed=include_closed,
                    )
                    if result.status != SUCCESS:
                        state.failed.append((name, result.error_message or result.status))
                except Exception as exc:
                    logger.exception("workflow %s: 전체 다시 수집 중 예외", workflow_id)
                    state.failed.append((name, f"{type(exc).__name__}: {exc}"))
                finally:
                    _running.discard(workflow_id)
                    scheduler.sync(conn)
            finally:
                conn.close()
                state.done += 1
    finally:
        state.running = False
        state.current = ""
        state.finished_at = _now()
        logger.info(
            "전체 원문 다시 수집이 끝났다: %s/%s곳, 실패 %s곳, 건너뜀 %s곳",
            state.done,
            state.total,
            len(state.failed),
            len(state.skipped),
        )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
