"""매일 공고 분석을 스케줄러 잡 하나로 건다. 시각은 표시 시간대(기본 한국) 기준이다.

미래내일 일경험 매일 수집(`app/work_experience/schedule.py`)과 같은 방법이다. 켜기·시각은
`app_settings` 에 있고, 화면에서 저장할 때와 기동할 때 이 함수로 맞춘다. 잡이 깨면 그날의 멈춤
시각을 정해 실행에 넘긴다.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app import db
from app.config import get_settings
from app.job_analysis import settings as analysis_settings
from app.job_analysis.runner import SCHEDULE, run_once

logger = logging.getLogger(__name__)

JOB_ID = "job_analysis:daily"


def stop_at(stop_time: str, now: datetime | None = None) -> datetime:
    """오늘의 멈춤 시각. 표시 시간대 기준이다."""
    zone = ZoneInfo(get_settings().display_timezone)
    today = (now or datetime.now(zone)).astimezone(zone)
    return today.replace(
        hour=int(stop_time[:2]), minute=int(stop_time[3:]), second=0, microsecond=0
    )


async def _execute() -> None:
    conn = db.connect()
    try:
        config = analysis_settings.read_config(conn)
        await run_once(conn, trigger=SCHEDULE, stop_at=stop_at(config.stop_time))
    finally:
        conn.close()


def sync(scheduler: AsyncIOScheduler, conn: sqlite3.Connection) -> bool:
    """설정대로 잡을 걸거나 뗀다. 걸려 있으면 True."""
    config = analysis_settings.read_config(conn)
    existing = scheduler.get_job(JOB_ID)
    if not config.schedule_enabled:
        if existing is not None:
            scheduler.remove_job(JOB_ID)
            logger.info("매일 공고 분석을 뗐다")
        return False
    scheduler.add_job(
        _execute,
        trigger=CronTrigger(
            hour=config.hour, minute=config.minute, timezone=get_settings().display_timezone
        ),
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    logger.info("공고 분석을 매일 %s 에 건다 (%s 에 멈춤)", config.start_time, config.stop_time)
    return True


def next_run_time(scheduler: AsyncIOScheduler) -> object | None:
    job = scheduler.get_job(JOB_ID)
    return getattr(job, "next_run_time", None) if job is not None else None
