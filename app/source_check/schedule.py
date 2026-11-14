"""상시 채용 원문 확인을 매일 한 번 건다 (LC-3621). 시각은 표시 시간대(기본 한국) 기준이다.

서버는 08:00~14:00 에만 켜진다. 매일 수집(08:30~11:00)과 공고 분석(11:10~11:50)이 끝난 뒤 12:00
에 시작해, 13:30 공고 현황 보고와 14:00 서버 끄기 전인 13:20 에 멈춘다. 다 못 본 공고는 다음 날
이어서 본다(`app/source_check/runner.py`). 화면에서 바꿀 일이 드물어 시각은 코드에 둔다.
"""

from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app import db
from app.config import get_settings
from app.job_analysis.schedule import stop_at
from app.source_check.runner import run_once

logger = logging.getLogger(__name__)

JOB_ID = "source_check:daily"
START_HOUR = 12
START_MINUTE = 0
STOP_TIME = "13:20"


async def _execute() -> None:
    conn = db.connect()
    try:
        await run_once(conn, stop_at=stop_at(STOP_TIME))
    finally:
        conn.close()


def sync(scheduler: AsyncIOScheduler) -> None:
    scheduler.add_job(
        _execute,
        trigger=CronTrigger(
            hour=START_HOUR, minute=START_MINUTE, timezone=get_settings().display_timezone
        ),
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )
    logger.info(
        "상시 채용 원문 확인을 매일 %02d:%02d 에 건다 (%s 에 멈춤)",
        START_HOUR,
        START_MINUTE,
        STOP_TIME,
    )
