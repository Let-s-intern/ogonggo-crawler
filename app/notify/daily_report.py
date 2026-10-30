"""매일 정해진 시각에 오늘 들어온 공고 수와 오공고로 보낸 공고 수를 슬랙으로 보낸다.

건수는 공고 목록 화면의 숫자를 그대로 쓴다 (`app/api/review_filter.py`). 따로 세면 슬랙과 화면의
숫자가 어긋난 날 어느 쪽이 맞는지부터 따져야 한다. 부트캠프·일경험은 화면처럼 세지 않는다.

0건인 날도 보낸다. 수집이 멈춘 날이 조용히 지나가면 안 된다.

웹훅 주소는 그 자체로 비밀이라 환경변수에 두고 로그에 적지 않는다. 비어 있으면 잡을 걸지 않는다.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app import db
from app.api.review_filter import VIEW_TODAY, JobFilter, count, sent_today
from app.api.ui import display_zone
from app.config import get_settings
from app.notify.ntfy import TIMEOUT_SECONDS, SendResult

logger = logging.getLogger(__name__)

JOB_ID = "notify:daily_report"

# 하루 한 번뿐이라 몇 분 늦게 깨어나도 그날 보고는 나간다
MISFIRE_GRACE_SECONDS = 600


def build_text(*, day: datetime, crawled: int, sent: int) -> str:
    return (
        f"*{day.month}/{day.day} 공고 현황*\n"
        f"• 오늘 들어온 공고: {crawled}건\n"
        f"• 오공고로 보낸 공고: {sent}건"
    )


async def send(
    webhook_url: str,
    text: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SendResult:
    """슬랙 웹훅으로 한 건 보낸다. 어떤 실패에서도 예외를 던지지 않는다."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(TIMEOUT_SECONDS), transport=transport
        ) as client:
            response = await client.post(webhook_url, json={"text": text})
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        logger.warning("슬랙이 %s 로 거절했다", status)
        return SendResult(ok=False, detail=f"슬랙이 {status} 로 거절했다", status_code=status)
    except Exception as exc:
        # 예외 문구에 웹훅 주소가 섞일 수 있어 종류만 남긴다
        logger.warning("슬랙으로 보내지 못했다: %s", type(exc).__name__)
        return SendResult(ok=False, detail=f"슬랙으로 보내지 못했다: {type(exc).__name__}")
    return SendResult(ok=True, detail="슬랙으로 보냈다", status_code=response.status_code)


async def report(
    conn: sqlite3.Connection,
    webhook_url: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SendResult:
    text = build_text(
        day=datetime.now(display_zone()),
        crawled=count(conn, JobFilter(view=VIEW_TODAY)),
        sent=sent_today(conn),
    )
    return await send(webhook_url, text, transport=transport)


async def _execute() -> None:
    conn = db.connect()
    try:
        result = await report(conn, get_settings().daily_report_slack_webhook)
    finally:
        conn.close()
    logger.info("일일 공고 현황: %s", result.detail)


def sync(scheduler: AsyncIOScheduler) -> bool:
    """설정대로 잡을 건다. 걸었으면 True."""
    settings = get_settings()
    if not settings.daily_report_slack_webhook:
        return False
    hour, minute = settings.daily_report_time.split(":")
    scheduler.add_job(
        _execute,
        trigger=CronTrigger(hour=int(hour), minute=int(minute), timezone=display_zone()),
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=MISFIRE_GRACE_SECONDS,
        replace_existing=True,
    )
    logger.info("일일 공고 현황을 매일 %s 에 슬랙으로 보낸다", settings.daily_report_time)
    return True
