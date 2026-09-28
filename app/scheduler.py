"""APScheduler 등록과 갱신.

`workflows` 테이블이 진실이다. 스케줄러가 들고 있는 잡 목록은 테이블의 사본일 뿐이고, 둘이
어긋나면 테이블 쪽으로 맞춘다 (`.claude/rules/crawling.md`).

그래서 등록도 갱신도 `sync()` 하나로 한다. 기동 시에도, 주기나 상태가 바뀐 뒤에도 같은 함수를
부른다 — "이 워크플로우만 다시 등록" 같은 부분 갱신 경로를 따로 두면 그 경로가 빠뜨린 변경이
스케줄러 메모리에만 남는다.

크롤 워크플로우는 저마다 잡을 갖지 않는다. 매일 정해진 시각에 깨어나는 잡 하나(`DAILY_JOB_ID`)가
그 시점의 `workflows` 를 읽어 active 인 곳을 한 곳씩 돈다 (`app/crawler/daily.py`). 서버가
하루 3~4시간만 켜져 있어서, 기동 시점부터 세는 주기 잡으로는 하루 주기가 한 번도 깨어나지 않는다.

`side_workflows` 에서 `active` 이면서 `interval` 인 행은 부가 잡이 된다. 한 번의 `sync()` 가
둘을 함께 맞추는 것은 부분 갱신 경로를 두지 않는 것과 같은 이유다 — 표 하나만 보는 동기화가
생기면 다른 표의 변경이 스케줄러에 늦게 온다.

동시 실행 상한도 여기 있다. 상한은 `app_settings` 에 저장되고 어드민에서 바뀌므로 고정 크기
세마포어를 쓸 수 없다 — `RunGate` 가 획득할 때마다 현재 값을 다시 읽는다
(`.claude/docs/architecture.md` 의 "동시 실행 상한").
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from apscheduler.events import EVENT_JOB_MAX_INSTANCES, JobSubmissionEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app import db, settings
from app.crawler import daily
from app.crawler.runner import SCHEDULE, run_workflow
from app.side import store as side_store
from app.side.runner import SCHEDULE as SIDE_SCHEDULE
from app.side.runner import run_now as run_side_now

logger = logging.getLogger(__name__)

# 매일 한 바퀴 수집 잡. 하나뿐이다
DAILY_JOB_ID = "daily:crawl"
# 부가 잡 id 의 앞머리
SIDE_JOB_PREFIX = "side:"

RunFn = Callable[[int], Awaitable[None]]
SleepFn = Callable[[float], Awaitable[None]]
ClockFn = Callable[[], datetime]


def side_job_id(side_workflow_id: int) -> str:
    return f"{SIDE_JOB_PREFIX}{side_workflow_id}"


def side_workflow_id_of(job_identifier: str) -> int | None:
    """잡 id 에서 부가 워크플로우 id 를 되읽는다. 부가 잡이 아니면 None."""
    return _id_after(SIDE_JOB_PREFIX, job_identifier)


def _id_after(prefix: str, job_identifier: str) -> int | None:
    """앞머리 뒤가 숫자일 때만 id 다.

    남의 잡을 우리 것으로 읽지 않는 것이 이 함수가 하는 일 전부다. 앞머리가 맞아도 뒤가
    숫자가 아니면 None 이고, 그 잡은 `sync()` 가 건드리지 않는다.
    """
    if not job_identifier.startswith(prefix):
        return None
    tail = job_identifier[len(prefix) :]
    return int(tail) if tail.isdigit() else None


@dataclass
class SyncReport:
    """`sync()` 가 테이블에 맞춘 결과. 로그와 테스트가 읽는다.

    `daily` 는 매일 한 바퀴 잡을 새로 걸었거나 시각을 바꿨으면 그 시각(`HH:MM`)이다.
    """

    daily: str = ""
    side_added: list[int] = field(default_factory=list)
    side_updated: list[int] = field(default_factory=list)
    side_removed: list[int] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.daily or self.side_added or self.side_updated or self.side_removed)


class RunGate:
    """동시에 도는 실행 수를 상한 이하로 유지하는 문 하나.

    `asyncio.Semaphore` 가 아닌 이유는 상한이 운영 중에 바뀌기 때문이다. 세마포어는 만들 때
    크기가 정해지므로 값이 바뀔 때마다 다시 만들어야 하고, 그 순간 이미 획득한 실행의 수를
    잃는다.

    상한은 획득하는 시점에 읽는다. 그래서 바뀐 값이 다음 획득부터 적용되고, 이미 돌고 있는
    실행은 상한이 내려가도 끊기지 않는다 — 상한을 줄이는 것은 새 실행을 늦추는 결정이지
    도중에 있는 실행을 버리는 결정이 아니다.
    """

    def __init__(self, limit: Callable[[], int]) -> None:
        self._limit = limit
        self._active = 0
        self._condition = asyncio.Condition()

    @property
    def active(self) -> int:
        return self._active

    def limit(self) -> int:
        return self._limit()

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        async with self._condition:
            # 상한이 올라갔을 수 있다. 기다리던 쪽에 다시 확인할 기회를 준다
            self._condition.notify_all()
            if not self._has_room():
                logger.info(
                    "동시 실행 상한(%s)에 걸려 대기한다. 진행 중=%s", self._limit(), self._active
                )
            await self._condition.wait_for(self._has_room)
            self._active += 1
        try:
            yield
        finally:
            async with self._condition:
                self._active -= 1
                self._condition.notify_all()

    def _has_room(self) -> bool:
        return self._active < self._limit()


def _configured_limit() -> int:
    """현재 상한. `app_settings` 가 진실이고, 값이 없으면 환경변수에서 채워진다."""
    conn = db.connect()
    try:
        return settings.read_int(conn, settings.MAX_CONCURRENT_RUNS)
    finally:
        conn.close()


_gate: RunGate | None = None


def get_gate() -> RunGate:
    """전역 문 하나. 상한은 이것을 모두가 공유할 때만 사실이다."""
    global _gate
    if _gate is None:
        _gate = RunGate(_configured_limit)
    return _gate


class WorkflowScheduler:
    """매일 한 바퀴 잡과 부가 잡을 APScheduler 에 거는 얇은 층.

    `runner`, `side_runner`, `sleep`, `clock` 은 테스트가 갈아끼운다. 운영에서는 `_execute`,
    `_execute_side`, `asyncio.sleep`, 현재 시각이다.
    """

    def __init__(
        self,
        *,
        scheduler: AsyncIOScheduler | None = None,
        runner: RunFn | None = None,
        side_runner: RunFn | None = None,
        sleep: SleepFn | None = None,
        clock: ClockFn | None = None,
    ) -> None:
        self._scheduler = scheduler or AsyncIOScheduler(timezone="UTC")
        self._runner = runner or self._execute
        self._side_runner = side_runner or self._execute_side
        self._sleep = sleep or asyncio.sleep
        self._clock = clock or (lambda: datetime.now(UTC))
        # 도는 중인 한 바퀴에서 아직 시작하지 않은 곳의 예정 시각. 화면이 읽는다
        self._round_slots: dict[int, datetime] = {}
        self._scheduler.add_listener(_log_skipped_tick, EVENT_JOB_MAX_INSTANCES)

    async def _run_round(self) -> None:
        """매일 한 바퀴. 오늘 아직 돌지 않은 곳을 id 순서로 한 곳씩 돈다.

        시작 간격은 남은 시간을 남은 곳 수로 나눈 값이다. 한 곳이 간격보다 오래 걸리면 다음
        곳은 끝나는 대로 바로 시작한다 — 두 곳이 겹쳐 돌지 않는 것이 간격보다 우선이다.

        차례가 온 곳이 그 사이 중지됐으면 건너뛴다. 무엇을 돌지는 잡이 아니라 표가 정한다.
        """
        conn = db.connect()
        try:
            config = daily.read_config(conn)
            now = self._clock()
            _, end = daily.round_window(config, now)
            remaining = daily.remaining_today(conn, config, now)
        finally:
            conn.close()
        if not remaining:
            logger.info("매일 수집: 오늘 돌 곳이 남아 있지 않다")
            return

        self._round_slots = daily.slots(remaining, now, end)
        logger.info(
            "매일 수집 시작: %d곳을 %s 까지 한 곳씩 돈다",
            len(remaining),
            end.astimezone(daily.zone()).strftime("%H:%M"),
        )
        try:
            for workflow_id in remaining:
                wait = (self._round_slots[workflow_id] - self._clock()).total_seconds()
                if wait > 0:
                    await self._sleep(wait)
                self._round_slots.pop(workflow_id, None)
                if not self._still_active(workflow_id):
                    logger.info("매일 수집: workflow %s 는 중지돼 건너뛴다", workflow_id)
                    continue
                try:
                    await self._runner(workflow_id)
                except Exception:
                    # 한 곳이 죽어도 남은 곳은 돈다. 실패는 실행 기록에 이미 남는다
                    logger.exception("매일 수집: workflow %s 실행이 예외로 끝났다", workflow_id)
        finally:
            self._round_slots = {}
        logger.info("매일 수집 끝: %d곳", len(remaining))

    @staticmethod
    def _still_active(workflow_id: int) -> bool:
        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT status FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
        finally:
            conn.close()
        return row is not None and row["status"] == "active"

    async def _execute(self, workflow_id: int) -> None:
        """잡 하나의 기본 실행 경로. 상한을 얻은 뒤에 연결을 연다.

        끝나고 다시 `sync()` 한다. 연속 실패로 자동 중지된 워크플로우는 테이블에서 `paused` 가
        되는데, 그 사실이 잡 목록까지 오지 않으면 멈춘 워크플로우가 계속 깨어난다.
        """
        async with get_gate().slot():
            conn = db.connect()
            try:
                await run_workflow(conn, workflow_id, trigger=SCHEDULE)
            finally:
                self.sync(conn)
                conn.close()

    async def _execute_side(self, side_workflow_id: int) -> None:
        """부가 잡 하나의 기본 실행 경로.

        **크롤 동시 실행 상한(`RunGate`)을 잡지 않는다.** 분류도 전달도 대상 사이트에 요청을
        보내지 않으므로, 크롤 슬롯을 하나 차지해 봐야 지켜지는 것은 없고 수집만 밀린다. 겹침
        방지는 자기 워크플로우에만 걸리고 그것은 실행기 안에 이미 있다 (`app/side/runner.py`
        의 `claim`).

        일은 다른 스레드에서 시킨다. `run_now` 는 동기 함수이고 그 안에서 `asyncio.run` 을
        부르므로 이벤트 루프 위에서 그대로 부르면 RuntimeError 로 죽는다. 스레드로 넘기면
        루프도 막히지 않아 분류가 도는 동안 크롤 잡이 제 시각에 깨어난다.
        """
        conn = db.connect()
        try:
            await asyncio.to_thread(run_side_now, conn, side_workflow_id, trigger=SIDE_SCHEDULE)
        finally:
            conn.close()

    @property
    def scheduler(self) -> AsyncIOScheduler:
        return self._scheduler

    def start(self, conn: sqlite3.Connection) -> SyncReport:
        """기동. 두 표에서 지금 돌아야 할 것을 전부 등록한다.

        오늘 한 바퀴 시간 안에 떴고 아직 돌지 않은 곳이 있으면 한 바퀴를 바로 시작한다. 서버를
        시작 시각보다 늦게 켠 날에 cron 잡은 내일 시각을 가리키므로, 그대로 두면 그날은 아무
        곳도 돌지 않는다.
        """
        if not self._scheduler.running:
            self._scheduler.start()
        report = self.sync(conn)
        self.catch_up(conn)
        return report

    def catch_up(self, conn: sqlite3.Connection) -> bool:
        """오늘 한 바퀴를 놓쳤으면 지금 시작한다. 시작했으면 True."""
        config = daily.read_config(conn)
        now = self._clock()
        begin, end = daily.round_window(config, now)
        if not begin <= now < end or not daily.remaining_today(conn, config, now):
            return False
        self._scheduler.modify_job(DAILY_JOB_ID, next_run_time=now)
        logger.info("매일 수집: 오늘 한 바퀴를 놓쳐 지금 시작한다")
        return True

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def sync(self, conn: sqlite3.Connection) -> SyncReport:
        """매일 한 바퀴 잡의 시각과 부가 잡을 설정·표에 맞춘다.

        남의 잡은 부가 표에 없지만 지우지 않는다 — 표에 없다는 것만으로 지우면 이 스케줄러에
        다른 용도로 붙은 잡이 사라진다.
        """
        wanted_side = _active_side_workflows(conn)

        report = SyncReport()
        report.daily = self._sync_daily(daily.read_config(conn))
        for job in list(self._scheduler.get_jobs()):
            side_workflow_id = side_workflow_id_of(job.id)
            if side_workflow_id is not None:
                self._settle(
                    job, side_workflow_id, wanted_side, report.side_updated, report.side_removed
                )

        for side_workflow_id, minutes in sorted(wanted_side.items()):
            self._add(side_job_id(side_workflow_id), self._side_runner, side_workflow_id, minutes)
            report.side_added.append(side_workflow_id)

        if report:
            logger.info(
                "scheduler sync: daily=%s side_added=%s side_updated=%s side_removed=%s",
                report.daily,
                report.side_added,
                report.side_updated,
                report.side_removed,
            )
        return report

    def _sync_daily(self, config: daily.DailyConfig) -> str:
        """매일 한 바퀴 잡을 설정 시각에 건다. 새로 걸었거나 시각이 바뀌었으면 그 시각."""
        job = self._scheduler.get_job(DAILY_JOB_ID)
        if job is not None and _cron_time(job) == config.start_time:
            return ""
        trigger = CronTrigger(hour=config.hour, minute=config.minute, timezone=daily.zone())
        # 몇 분 늦게 깨어나도 그날 한 바퀴는 돈다
        grace = max(60, config.spread_minutes * 60)
        if job is not None:
            # 기동 전 스케줄러에서 `add_job(replace_existing=True)` 는 옛 잡을 대기열에 남긴다
            self._scheduler.modify_job(DAILY_JOB_ID, misfire_grace_time=grace)
            self._scheduler.reschedule_job(DAILY_JOB_ID, trigger=trigger)
            return config.start_time
        self._scheduler.add_job(
            self._run_round,
            trigger=trigger,
            id=DAILY_JOB_ID,
            # 한 바퀴가 다음 날 시작 시각까지 이어져도 두 바퀴가 겹치지 않는다
            max_instances=1,
            coalesce=True,
            misfire_grace_time=grace,
        )
        return config.start_time

    def _settle(
        self,
        job: object,
        identifier: int,
        wanted: dict[int, int],
        updated: list[int],
        removed: list[int],
    ) -> None:
        """이미 등록된 부가 잡 하나를 표에 맞춘다. 맞춘 행은 `wanted` 에서 뺀다."""
        minutes = wanted.pop(identifier, None)
        if minutes is None:
            # 멈췄거나, 주기가 아니게 됐거나, 행이 사라졌다. 어느 쪽이든 더 이상 깨우지 않는다
            self._scheduler.remove_job(job.id)  # type: ignore[attr-defined]
            removed.append(identifier)
        elif _interval_minutes(job) != minutes:
            self._scheduler.reschedule_job(
                job.id,  # type: ignore[attr-defined]
                trigger=IntervalTrigger(minutes=minutes),
            )
            updated.append(identifier)

    def side_scheduled(self) -> dict[int, int]:
        """등록된 (부가 워크플로우 id -> 주기 분)."""
        found: dict[int, int] = {}
        for job in self._scheduler.get_jobs():
            side_workflow_id = side_workflow_id_of(job.id)
            if side_workflow_id is not None:
                found[side_workflow_id] = _interval_minutes(job)
        return found

    def next_run_times(self, conn: sqlite3.Connection) -> dict[int, datetime]:
        """(워크플로우 id -> 다음 실행 예정 시각). 화면이 "언제 도는가" 에 답하는 값이다.

        도는 중인 한 바퀴에 남은 곳은 그 바퀴가 정한 시각이다. 나머지는 매일 잡의 다음 시각에
        순번만큼 간격을 더한 값이다 — 앞 곳이 늦어지면 밀리므로 예정일 뿐이다.

        매일 잡이 없거나 기동 전이라 다음 시각이 아직 없으면 도는 중인 곳만 돌려준다. 모르는
        것은 모른다고 적는 편이 틀린 시각보다 낫다.
        """
        found = dict(self._round_slots)
        job = self._scheduler.get_job(DAILY_JOB_ID)
        base = getattr(job, "next_run_time", None) if job is not None else None
        if base is None:
            return found
        config = daily.read_config(conn)
        order = daily.active_workflows(conn)
        planned = daily.slots(order, base, base + timedelta(minutes=config.spread_minutes))
        for workflow_id, when in planned.items():
            found.setdefault(workflow_id, when)
        return found

    def _add(self, identifier: str, runner: RunFn, argument: int, minutes: int) -> None:
        """부가 잡 하나를 등록한다."""
        self._scheduler.add_job(
            runner,
            trigger=IntervalTrigger(minutes=minutes),
            args=[argument],
            id=identifier,
            # 앞 실행이 끝나지 않았으면 이번 tick 은 건너뛴다. 한 워크플로우의 실행이
            # 둘 동시에 뜨지 않는다
            max_instances=1,
            # 프로세스가 멈춰 tick 을 여러 번 놓쳤어도 밀린 만큼 몰아서 돌지 않는다
            coalesce=True,
            replace_existing=True,
        )


def _active_side_workflows(conn: sqlite3.Connection) -> dict[int, int]:
    """지금 돌아야 할 (부가 워크플로우 id -> 주기 분).

    `interval` 이 아닌 것은 주기 값이 적혀 있어도 등록하지 않는다. `after_crawl` 은 크롤이
    끝난 자리가 부르고 `manual` 은 화면이 부른다 — 여기서 함께 등록하면 운영자가 고르지 않은
    주기로도 도는 것이 된다.
    """
    return {
        workflow.id: workflow.interval_minutes
        for workflow in side_store.list_all(conn)
        if workflow.status == side_store.ACTIVE and workflow.trigger_kind == side_store.INTERVAL
    }


def _cron_time(job: object) -> str:
    """매일 잡의 `CronTrigger` 가 가리키는 시각 `HH:MM`. 읽지 못하면 빈 문자열."""
    trigger = getattr(job, "trigger", None)
    fields = {part.name: str(part) for part in getattr(trigger, "fields", [])}
    try:
        return f"{int(fields['hour']):02d}:{int(fields['minute']):02d}"
    except (KeyError, ValueError):
        return ""


def _interval_minutes(job: object) -> int:
    """`IntervalTrigger` 의 주기를 분으로 읽는다."""
    trigger = getattr(job, "trigger", None)
    interval = getattr(trigger, "interval", None)
    if interval is None:
        return 0
    return int(interval.total_seconds() // 60)


def _log_skipped_tick(event: JobSubmissionEvent) -> None:
    """앞 실행이 아직 돌고 있어 건너뛴 tick. 건너뛴 사실은 반드시 남는다.

    `EVENT_JOB_MAX_INSTANCES` 는 실행이 아니라 제출이 막힌 사건이라 `JobSubmissionEvent` 로
    온다. 넘어오는 시각도 하나가 아니라 목록(`scheduled_run_times`)이다.

    부가 잡의 겹침은 `side_runs` 에 건너뜀 행으로도 남지만, 그것은
    실행 함수까지 들어온 차례의 이야기다. 여기서 막힌 차례는 실행 함수에 닿지도 못해서
    적어 두지 않으면 어디에도 남지 않는다.
    """
    if event.job_id == DAILY_JOB_ID:
        logger.warning(
            "매일 수집: 앞 바퀴가 끝나지 않아 이번 바퀴를 건너뛴다 (scheduled_at=%s)",
            ", ".join(str(when) for when in event.scheduled_run_times),
        )
        return
    side_workflow_id = side_workflow_id_of(event.job_id)
    if side_workflow_id is not None:
        _log_skipped("side workflow", side_workflow_id, event)


def _log_skipped(kind: str, identifier: int, event: JobSubmissionEvent) -> None:
    logger.warning(
        "%s %s: 앞 실행이 끝나지 않아 이번 tick 을 건너뛴다 (scheduled_at=%s)",
        kind,
        identifier,
        ", ".join(str(when) for when in event.scheduled_run_times),
    )


_scheduler: WorkflowScheduler | None = None


def get_scheduler() -> WorkflowScheduler:
    """앱이 쓰는 인스턴스 하나."""
    global _scheduler
    if _scheduler is None:
        _scheduler = WorkflowScheduler()
    return _scheduler


def shutdown_scheduler() -> None:
    global _scheduler, _gate
    if _scheduler is not None:
        _scheduler.shutdown()
        _scheduler = None
    _gate = None
