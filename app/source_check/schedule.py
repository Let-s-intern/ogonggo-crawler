"""상시 채용 마감 여부 확인을 매일 한 번 건다 (LC-3621). 시각은 표시 시간대(기본 한국) 기준이다.

서버는 08:00~14:00 에만 켜진다. 오공고에 이미 있는 공고만 보므로 오늘 수집을 기다리지 않고 매일
수집과 같은 08:30 에 함께 시작해, 13:30 공고 현황 보고와 14:00 서버 끄기 전인 13:20 에 멈춘다
(2026-11-17, 일이 차례로 돌아 CPU 가 놀았다). 원문 요청은 공용 fetch 클라이언트의 호스트별 딜레이를
지키므로 수집과 같은 사이트에 몰리지 않는다. 다 못 본 공고는 다음 날 이어서 본다
(`app/source_check/runner.py`). 화면에서 바꿀 일이 드물어 시각은 코드에 둔다.
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
START_HOUR = 8
START_MINUTE = 30
STOP_TIME = "13:20"
# 시작 시각에 이벤트 루프가 바빠 몇 분 늦게 깨어나도 그날은 돈다. 기본값(1초)이면 그날을 소리 없이
# 건너뛴다. 멈춤 시각을 넘겨 깨면 `run_once` 가 바로 멈춘다
MISFIRE_GRACE_SECONDS = 4 * 60 * 60


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
        misfire_grace_time=MISFIRE_GRACE_SECONDS,
        replace_existing=True,
    )
    logger.info(
        "상시 채용 마감 여부 확인을 매일 %02d:%02d 에 건다 (%s 에 멈춤)",
        START_HOUR,
        START_MINUTE,
        STOP_TIME,
    )
