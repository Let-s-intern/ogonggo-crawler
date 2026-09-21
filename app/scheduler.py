"""APScheduler 등록과 갱신.

`workflows` 테이블이 진실이다. 스케줄러가 들고 있는 잡 목록은 테이블의 사본일 뿐이고, 둘이
어긋나면 테이블 쪽으로 맞춘다 (`.claude/rules/crawling.md`).

그래서 등록도 갱신도 `sync()` 하나로 한다. 기동 시에도, 주기나 상태가 바뀐 뒤에도 같은 함수를
부른다 — "이 워크플로우만 다시 등록" 같은 부분 갱신 경로를 따로 두면 그 경로가 빠뜨린 변경이
스케줄러 메모리에만 남는다.

무엇을 실행할지도 잡이 아니라 테이블이 정한다. 잡에 실려 있는 것은 워크플로우 id 뿐이고,
URL 과 셀렉터는 실행 시점에 `app/crawler/runner.py` 가 다시 읽는다.

맞추는 표가 둘이다. `workflows` 는 크롤 잡이 되고, `side_workflows` 에서 `active` 이면서
`interval` 인 행은 부가 잡이 된다. 두 표는 저마다 자동 증가라 1번이 둘 있으므로 잡 id 의
앞머리로 가른다. 한 번의 `sync()` 가 둘을 함께 맞추는 것은 부분 갱신 경로를 두지 않는 것과
같은 이유다 — 표 하나만 보는 동기화가 생기면 다른 표의 변경이 스케줄러에 늦게 온다.

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
from datetime import datetime

from apscheduler.events import EVENT_JOB_MAX_INSTANCES, JobSubmissionEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app import db, settings
from app.crawler.runner import SCHEDULE, run_workflow
from app.side import store as side_store
from app.side.runner import SCHEDULE as SIDE_SCHEDULE
from app.side.runner import run_now as run_side_now

logger = logging.getLogger(__name__)

# 잡 id 의 앞머리. 종류마다 다르다.
#
# `workflows.id` 와 `side_workflows.id` 는 서로 다른 표의 자동 증가 값이라 1번이 둘 있다.
# 앞머리가 하나뿐이면 두 종류의 1번이 같은 잡 id 를 쓰고, `add_job(replace_existing=True)` 가
# 먼저 있던 크롤 잡을 말없이 덮는다 — 크롤이 멈춘 것이 아무 데도 안 남는다
JOB_PREFIX = "workflow:"
SIDE_JOB_PREFIX = "side:"

RunFn = Callable[[int], Awaitable[None]]


def job_id(workflow_id: int) -> str:
    return f"{JOB_PREFIX}{workflow_id}"


def side_job_id(side_workflow_id: int) -> str:
    return f"{SIDE_JOB_PREFIX}{side_workflow_id}"


def workflow_id_of(job_identifier: str) -> int | None:
    """잡 id 에서 워크플로우 id 를 되읽는다. 크롤 잡이 아니면 None."""
    return _id_after(JOB_PREFIX, job_identifier)


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

    두 종류를 한 목록에 섞지 않는다. `workflows` 3번과 `side_workflows` 3번은 다른 것이고,
    섞으면 로그를 읽는 사람이 무엇이 등록됐는지 알 수 없다.
    """

    added: list[int] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)
    removed: list[int] = field(default_factory=list)
    side_added: list[int] = field(default_factory=list)
    side_updated: list[int] = field(default_factory=list)
    side_removed: list[int] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(
            self.added
            or self.updated
            or self.removed
            or self.side_added
            or self.side_updated
            or self.side_removed
        )


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
    """`workflows` 를 APScheduler 잡으로 옮기는 얇은 층.

    `runner` 와 `side_runner` 는 테스트가 갈아끼운다. 운영에서는 `_execute` 와
    `_execute_side` 다.
    """

    def __init__(
        self,
        *,
        scheduler: AsyncIOScheduler | None = None,
        runner: RunFn | None = None,
        side_runner: RunFn | None = None,
    ) -> None:
        self._scheduler = scheduler or AsyncIOScheduler(timezone="UTC")
        self._runner = runner or self._execute
        self._side_runner = side_runner or self._execute_side
        self._scheduler.add_listener(_log_skipped_tick, EVENT_JOB_MAX_INSTANCES)

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
        """기동. 두 표에서 지금 돌아야 할 것을 전부 등록한다."""
        if not self._scheduler.running:
            self._scheduler.start()
        return self.sync(conn)

    def shutdown(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def sync(self, conn: sqlite3.Connection) -> SyncReport:
        """등록된 잡을 두 표에 맞춘다. 추가·주기 갱신·제거를 한 번에 한다.

        잡 하나하나를 크롤·부가·남의 것 셋으로 가른다. 남의 잡은 어느 쪽 표에도 없지만 지우지
        않는다 — 표에 없다는 것만으로 지우면 이 스케줄러에 다른 용도로 붙은 잡이 사라진다.
        """
        wanted = _active_workflows(conn)
        wanted_side = _active_side_workflows(conn)

        report = SyncReport()
        for job in list(self._scheduler.get_jobs()):
            workflow_id = workflow_id_of(job.id)
            if workflow_id is not None:
                self._settle(job, workflow_id, wanted, report.updated, report.removed)
                continue
            side_workflow_id = side_workflow_id_of(job.id)
            if side_workflow_id is not None:
                self._settle(
                    job, side_workflow_id, wanted_side, report.side_updated, report.side_removed
                )

        for workflow_id, minutes in sorted(wanted.items()):
            self._add(job_id(workflow_id), self._runner, workflow_id, minutes)
            report.added.append(workflow_id)

        for side_workflow_id, minutes in sorted(wanted_side.items()):
            self._add(side_job_id(side_workflow_id), self._side_runner, side_workflow_id, minutes)
            report.side_added.append(side_workflow_id)

        if report:
            logger.info(
                "scheduler sync: added=%s updated=%s removed=%s"
                " side_added=%s side_updated=%s side_removed=%s",
                report.added,
                report.updated,
                report.removed,
                report.side_added,
                report.side_updated,
                report.side_removed,
            )
        return report

    def _settle(
        self,
        job: object,
        identifier: int,
        wanted: dict[int, int],
        updated: list[int],
        removed: list[int],
    ) -> None:
        """이미 등록된 잡 하나를 표에 맞춘다. 맞춘 행은 `wanted` 에서 뺀다.

        두 종류가 같은 함수를 지난다. 지우는 규칙과 주기를 갱신하는 규칙이 갈리면, 한쪽에만
        고쳐진 채로 남는 날이 온다.
        """
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

    def scheduled(self) -> dict[int, int]:
        """등록된 (워크플로우 id -> 주기 분). 테스트와 진단이 읽는다."""
        found: dict[int, int] = {}
        for job in self._scheduler.get_jobs():
            workflow_id = workflow_id_of(job.id)
            if workflow_id is not None:
                found[workflow_id] = _interval_minutes(job)
        return found

    def side_scheduled(self) -> dict[int, int]:
        """등록된 (부가 워크플로우 id -> 주기 분).

        `scheduled()` 와 따로 둔다. 섞어 돌려주면 두 표의 1번이 한 칸을 다투고, 읽는 쪽은
        어느 종류의 1번인지 알 방법이 없다.
        """
        found: dict[int, int] = {}
        for job in self._scheduler.get_jobs():
            side_workflow_id = side_workflow_id_of(job.id)
            if side_workflow_id is not None:
                found[side_workflow_id] = _interval_minutes(job)
        return found

    def next_run_times(self) -> dict[int, datetime]:
        """등록된 (워크플로우 id -> 다음 실행 예정 시각). 화면이 "언제 도는가" 에 답하는 값이다.

        잡이 없거나 예정 시각이 아직 정해지지 않은 워크플로우는 빠진다. 마지막 실행에 주기를
        더해 대신 계산하지 않는다 — 잡의 tick 은 잡이 등록된 시점부터 세므로 프로세스가 다시
        뜬 뒤에는 그 계산이 틀린다. 모르는 것은 모른다고 적는 편이 틀린 시각보다 낫다.

        기동 전 스케줄러의 잡에는 `next_run_time` 속성 자체가 없다. 그래서 getattr 로 읽는다.
        """
        found: dict[int, datetime] = {}
        for job in self._scheduler.get_jobs():
            workflow_id = workflow_id_of(job.id)
            when = getattr(job, "next_run_time", None)
            if workflow_id is not None and when is not None:
                found[workflow_id] = when
        return found

    def _add(self, identifier: str, runner: RunFn, argument: int, minutes: int) -> None:
        """잡 하나를 등록한다. 두 종류가 같은 문장을 지난다.

        `max_instances` 와 `coalesce` 는 종류를 가리지 않는다. 부가 잡만 따로 등록하는
        문장을 두면 그 둘 중 하나가 부가 잡에만 빠지는 날이 온다.
        """
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


def _active_workflows(conn: sqlite3.Connection) -> dict[int, int]:
    """지금 돌아야 할 (크롤 워크플로우 id -> 주기 분).

    직접 넣은 공고의 워크플로우(`kind = 'manual'`)는 돌 목록이 없어 빼 둔다.
    """
    rows = conn.execute(
        "SELECT id, interval_minutes, status FROM workflows WHERE kind = 'crawl'"
    ).fetchall()
    return {
        int(row["id"]): int(row["interval_minutes"]) for row in rows if row["status"] == "active"
    }


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

    부가 잡도 같이 남긴다. 부가 잡의 겹침은 `side_runs` 에 건너뜀 행으로도 남지만, 그것은
    실행 함수까지 들어온 차례의 이야기다. 여기서 막힌 차례는 실행 함수에 닿지도 못해서
    적어 두지 않으면 어디에도 남지 않는다.
    """
    workflow_id = workflow_id_of(event.job_id)
    if workflow_id is not None:
        _log_skipped("workflow", workflow_id, event)
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
