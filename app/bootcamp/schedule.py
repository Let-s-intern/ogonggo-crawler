"""부트캠프 주기 수집을 스케줄러 잡 하나로 건다.

크롤 워크플로우와 같은 APScheduler 에 붙지만 앞머리가 달라 `WorkflowScheduler.sync()` 가 건드리지
않는다 — 그쪽은 모르는 잡을 지우지 않는다 (`app/scheduler.py`). 켜기·주기는 `app_settings` 에 있고,
화면에서 저장할 때와 기동할 때 이 함수로 맞춘다.
"""

from __future__ import annotations

import logging
import sqlite3

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app import db
from app.bootcamp import settings as bootcamp_settings
from app.bootcamp.runner import SCHEDULE, run_sesac
from app.scheduler import get_gate

logger = logging.getLogger(__name__)

JOB_ID = "bootcamp:sesac"


async def _execute() -> None:
    """새싹에 요청을 보내므로 크롤 동시 실행 상한을 함께 잡는다."""
    async with get_gate().slot():
        conn = db.connect()
        try:
            await run_sesac(conn, trigger=SCHEDULE)
        finally:
            conn.close()


def sync(scheduler: AsyncIOScheduler, conn: sqlite3.Connection) -> bool:
    """설정대로 잡을 걸거나 뗀다. 걸려 있으면 True."""
    config = bootcamp_settings.read_config(conn)
    existing = scheduler.get_job(JOB_ID)
    if not config.schedule_enabled:
        if existing is not None:
            scheduler.remove_job(JOB_ID)
            logger.info("부트캠프 주기 수집을 뗐다")
        return False
    scheduler.add_job(
        _execute,
        trigger=IntervalTrigger(hours=config.interval_hours),
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    logger.info("부트캠프 주기 수집을 %s시간마다 건다", config.interval_hours)
    return True


def next_run_time(scheduler: AsyncIOScheduler) -> object | None:
    job = scheduler.get_job(JOB_ID)
    return getattr(job, "next_run_time", None) if job is not None else None
