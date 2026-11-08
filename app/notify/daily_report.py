"""오늘 공고 수집의 결과를 슬랙으로 보낸다.

한 바퀴가 끝나면 수집 결과를, 남은 분류까지 끝나면 분류·전송 결과를 보낸다 (`finish_round`).
매일 정해진 시각에는 오늘 들어온 공고 수와 오공고로 보낸 공고 수를 보낸다. 앞의 두 통이 오지 않은
날을 알아채는 안전망이라 끝나지 않았으면 그 사실도 적는다 (2026-11-02 결정).

건수는 공고 목록 화면의 숫자를 그대로 쓴다 (`app/api/review_filter.py`). 따로 세면 슬랙과 화면의
숫자가 어긋난 날 어느 쪽이 맞는지부터 따져야 한다. 부트캠프·일경험은 화면처럼 세지 않는다.

0건인 날도 보낸다. 수집이 멈춘 날이 조용히 지나가면 안 된다.

웹훅 주소는 그 자체로 비밀이라 환경변수에 두고 로그에 적지 않는다. 비어 있으면 잡을 걸지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app import db
from app.api.review_filter import (
    VIEW_CHECK,
    VIEW_TODAY,
    JobFilter,
    count,
    sent_today,
    today_start_utc,
)
from app.api.ui import display_zone
from app.classify.batch import get_classify_run
from app.classify.store import pending_count
from app.config import get_settings
from app.notify import work_report
from app.notify.ntfy import SendResult
from app.notify.slack import send
from app.side import store as side_store
from app.side.runner import AFTER_CRAWL, classify_running, run_now

logger = logging.getLogger(__name__)

JOB_ID = "notify:daily_report"

# 하루 한 번뿐이라 몇 분 늦게 깨어나도 그날 보고는 나간다
MISFIRE_GRACE_SECONDS = 600

# 한 바퀴가 끝나고 돌고 있는 분류를 기다리는 시간과 들여다보는 간격
DRAIN_WAIT_SECONDS = 1800
DRAIN_POLL_SECONDS = 10
# 슬랙에 이름을 적을 실패 사이트 수
MAX_NAMES = 10
# 오늘 한 바퀴의 보고까지 끝났으면 그 날짜(`YYYY-MM-DD`)를 적는다
ROUND_FINISHED_DAY = "daily_round_finished_day"
UNFINISHED = "⚠️ 오늘 수집·분류가 아직 끝나지 않았다"

SleepFn = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class CrawlDay:
    """오늘 매일 수집의 결과. 다시 돈 사이트는 마지막 실행의 성패로 센다."""

    sites: int = 0
    failed_names: tuple[str, ...] = ()
    new: int = 0
    skipped: int = 0
    item_failures: int = 0
    repaired: int = 0
    started_at: str = ""
    finished_at: str = ""


def build_text(*, day: datetime, crawled: int, sent: int, warning: str = "") -> str:
    text = (
        f"*{day.month}/{day.day} 공고 현황*\n"
        f"• 오늘 들어온 공고: {crawled}건\n"
        f"• 오공고로 보낸 공고: {sent}건"
    )
    return f"{text}\n• {warning}" if warning else text


def _local_time(value: str) -> str:
    """DB 의 UTC 시각을 표시 시간대의 `HH:MM` 으로."""
    moment = datetime.fromisoformat(value).replace(tzinfo=UTC)
    return moment.astimezone(display_zone()).strftime("%H:%M")


def crawl_day(conn: sqlite3.Connection) -> CrawlDay:
    """오늘 스케줄 실행을 모은다. 값은 `crawl_runs` 와 `selector_auto_repairs` 에서 읽는다."""
    since = today_start_utc()
    rows = conn.execute(
        """
        SELECT w.name AS name, r.workflow_id AS workflow_id, r.status AS status,
               r.new_count AS new_count, r.skipped_count AS skipped_count,
               r.fail_count AS fail_count, r.started_at AS started_at,
               r.finished_at AS finished_at
          FROM crawl_runs r
          JOIN workflows w ON w.id = r.workflow_id
         WHERE r.trigger = 'schedule' AND r.started_at >= ?
         ORDER BY r.id
        """,
        (since,),
    ).fetchall()
    if not rows:
        return CrawlDay()
    latest = {int(row["workflow_id"]): row for row in rows}
    repaired = conn.execute(
        """
        SELECT count(*) FROM selector_auto_repairs
         WHERE trigger = 'schedule' AND outcome = 'repaired' AND created_at >= ?
        """,
        (since,),
    ).fetchone()[0]
    return CrawlDay(
        sites=len(latest),
        failed_names=tuple(
            str(row["name"]) for row in latest.values() if row["status"] != "success"
        ),
        new=sum(int(row["new_count"]) for row in rows),
        skipped=sum(int(row["skipped_count"]) for row in rows),
        item_failures=sum(int(row["fail_count"]) for row in rows),
        repaired=int(repaired),
        started_at=_local_time(str(rows[0]["started_at"])),
        finished_at=max(
            (_local_time(str(row["finished_at"])) for row in rows if row["finished_at"]),
            default="",
        ),
    )


def crawl_lines(day: CrawlDay) -> list[str]:
    failed = len(day.failed_names)
    lines = [
        f"시간: {day.started_at}~{day.finished_at}",
        f"사이트 {day.sites}곳: 성공 {day.sites - failed}, 실패 {failed}",
    ]
    if day.failed_names:
        names = ", ".join(day.failed_names[:MAX_NAMES])
        rest = failed - MAX_NAMES
        lines.append(f"실패 사이트: {names}" + (f" 외 {rest}곳" if rest > 0 else ""))
    lines.append(f"새 공고 {day.new}건 · 건너뜀 {day.skipped}건 · 항목 실패 {day.item_failures}건")
    if day.repaired:
        lines.append(f"셀렉터 자동 수리 {day.repaired}곳")
    return lines


def classify_lines(conn: sqlite3.Connection, note: str = "") -> list[str]:
    """오늘 분류와 전송의 결과. 수집 직후 분류를 모두 더한 값이다."""
    since = today_start_utc()
    classified = conn.execute(
        """
        SELECT coalesce(sum(r.processed_count), 0) AS processed,
               coalesce(sum(r.failed_count), 0) AS failed
          FROM side_runs r
          JOIN side_workflows w ON w.id = r.side_workflow_id
         WHERE w.kind = 'classify' AND r.started_at >= ?
        """,
        (since,),
    ).fetchone()
    rechecked = conn.execute(
        "SELECT count(*) FROM quality_gate_runs WHERE created_at >= ?", (since,)
    ).fetchone()[0]
    send_failed = conn.execute(
        "SELECT count(*) FROM spring_deliveries WHERE status = 'failed' AND updated_at >= ?",
        (since,),
    ).fetchone()[0]
    closed = conn.execute(
        "SELECT count(*) FROM job_sightings WHERE close_sent_at >= ? AND close_error IS NULL",
        (since,),
    ).fetchone()[0]
    waiting = count(conn, JobFilter(view=VIEW_CHECK))
    lines = [
        f"분류 {classified['processed']}건: 실패 {classified['failed']}건",
        f"품질 검사 재분류 {rechecked}건 · 확인 필요 전체 {waiting}건",
        f"오공고 전송 {sent_today(conn)}건: 실패 {send_failed}건",
        f"오공고 마감 {closed}건",
        f"남은 미분류 {pending_count(conn)}건",
    ]
    return [*lines, note] if note else lines


async def _drain(
    conn: sqlite3.Connection,
    *,
    sleep: SleepFn = asyncio.sleep,
    wait_seconds: int = DRAIN_WAIT_SECONDS,
) -> str:
    """돌고 있는 분류를 기다렸다가 남은 공고를 한 번 더 분류한다. 못 했으면 그 사유다.

    사이트의 분류가 돌고 있으면 다음 사이트의 수집 직후 분류는 물러난다. 마지막 쪽 사이트의 공고가
    다음 날까지 밀리지 않게 여기서 거둔다. 분류 끝에서 전송까지 이어진다 (`app/classify/batch.py`).
    """
    waited = 0
    while classify_running(conn) or get_classify_run().progress().running:
        if waited >= wait_seconds:
            return f"앞선 분류가 {wait_seconds // 60}분 안에 끝나지 않아 마지막 분류를 건너뛰었다"
        await sleep(DRAIN_POLL_SECONDS)
        waited += DRAIN_POLL_SECONDS
    for workflow in side_store.list_all(conn):
        if (
            workflow.kind == side_store.CLASSIFY
            and workflow.trigger_kind == AFTER_CRAWL
            and workflow.status == side_store.ACTIVE
        ):
            await asyncio.to_thread(run_now, conn, workflow.id, trigger=AFTER_CRAWL)
    return ""


def _mark_finished(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO app_settings (key, value) VALUES (?, ?)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')
        """,
        (ROUND_FINISHED_DAY, datetime.now(display_zone()).strftime("%Y-%m-%d")),
    )


def round_finished_today(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?", (ROUND_FINISHED_DAY,)
    ).fetchone()
    return row is not None and row["value"] == datetime.now(display_zone()).strftime("%Y-%m-%d")


async def finish_round(conn: sqlite3.Connection, *, sleep: SleepFn = asyncio.sleep) -> None:
    """매일 수집 한 바퀴의 끝. 수집 결과를 보내고 남은 분류를 마친 뒤 분류·전송 결과를 보낸다."""
    await work_report.report("공고 수집", crawl_lines(crawl_day(conn)))
    note = await _drain(conn, sleep=sleep)
    await work_report.report("공고 분류·전송", classify_lines(conn, note))
    _mark_finished(conn)


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
        warning="" if round_finished_today(conn) else UNFINISHED,
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
