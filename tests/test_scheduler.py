"""매일 한 바퀴 수집 테스트.

실제 시각을 기다리지 않는다. 시계와 잠자기를 갈아끼워 "지금이 이 시각이면 이렇게 돈다" 를 본다.
실사이트에 나가지 않는다. 한 곳을 도는 실행 함수는 워크플로우 id 만 받아 적는 스텁이다.
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import db
from app.crawler import daily
from app.scheduler import DAILY_JOB_ID, WorkflowScheduler

KST = ZoneInfo("Asia/Seoul")


def kst(hour: int, minute: int = 0, day: int = 28) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=KST).astimezone(UTC)


class Clock:
    """잠자기만큼 시각이 흐르는 가짜 시계."""

    def __init__(self, now: datetime) -> None:
        self.now = now
        self.slept: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def path(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    location = tmp_path / "jobs.db"
    connect = db.connect
    # 한 바퀴는 스스로 연결을 연다. 같은 파일을 보게 한다
    monkeypatch.setattr("app.scheduler.db.connect", lambda *_, **__: connect(location))
    return location


@pytest.fixture
def conn(path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(path)
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES (?, ?, 'promoted')",
        ("python.org", "https://www.python.org/jobs/"),
    )
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def calls() -> list[int]:
    return []


@pytest.fixture
def clock() -> Clock:
    return Clock(kst(8, 30))


@pytest.fixture
def scheduler(calls: list[int], clock: Clock) -> Iterator[WorkflowScheduler]:
    async def record(workflow_id: int) -> None:
        calls.append(workflow_id)
        # 한 곳에 1분 걸린다
        clock.now += timedelta(minutes=1)

    # 시작하지 않는다. 잡은 pending 으로 쌓이고 조회·갱신은 그대로 동작한다
    instance = WorkflowScheduler(
        scheduler=AsyncIOScheduler(timezone="UTC"),
        runner=record,
        sleep=clock.sleep,
        clock=clock,
    )
    try:
        yield instance
    finally:
        instance.shutdown()


def add_workflow(conn: sqlite3.Connection, name: str, status: str = "active") -> int:
    cursor = conn.execute(
        "INSERT INTO workflows (crawler_id, name, status) VALUES (1, ?, ?)", (name, status)
    )
    return int(cursor.lastrowid or 0)


def add_run(conn: sqlite3.Connection, workflow_id: int, when: datetime, trigger: str) -> None:
    conn.execute(
        "INSERT INTO crawl_runs (workflow_id, started_at, trigger) VALUES (?, ?, ?)",
        (workflow_id, when.strftime("%Y-%m-%d %H:%M:%S"), trigger),
    )


def test_매일_잡은_기본_08시_30분_한국_시각에_하나만_걸린다(
    scheduler: WorkflowScheduler, conn: sqlite3.Connection
) -> None:
    add_workflow(conn, "하나")
    add_workflow(conn, "둘")

    report = scheduler.sync(conn)

    assert report.daily == "08:30"
    jobs = scheduler.scheduler.get_jobs()
    assert [job.id for job in jobs] == [DAILY_JOB_ID]
    assert jobs[0].max_instances == 1
    fire = jobs[0].trigger.get_next_fire_time(None, kst(7, 0))
    assert fire == kst(8, 30)


def test_시각을_바꾸면_잡이_따라간다(
    scheduler: WorkflowScheduler, conn: sqlite3.Connection
) -> None:
    scheduler.sync(conn)
    daily.write_config(conn, daily.DailyConfig(start_time="07:10", spread_minutes=120))

    report = scheduler.sync(conn)

    assert report.daily == "07:10"
    job = scheduler.scheduler.get_job(DAILY_JOB_ID)
    assert job.trigger.get_next_fire_time(None, kst(6, 0)) == kst(7, 10)


def test_바뀐_것이_없으면_아무것도_하지_않는다(
    scheduler: WorkflowScheduler, conn: sqlite3.Connection
) -> None:
    scheduler.sync(conn)

    assert not scheduler.sync(conn)


def test_잘못된_설정은_거절한다(conn: sqlite3.Connection) -> None:
    with pytest.raises(daily.DailySettingError):
        daily.write_config(conn, daily.DailyConfig(start_time="8:30"))
    with pytest.raises(daily.DailySettingError):
        daily.write_config(conn, daily.DailyConfig(spread_minutes=-1))


async def test_한_곳씩_id_순서로_간격을_두고_돈다(
    scheduler: WorkflowScheduler, conn: sqlite3.Connection, calls: list[int], clock: Clock
) -> None:
    """150분을 세 곳으로 나누면 50분 간격이다. 한 곳이 1분 걸리면 49분을 쉰다."""
    first = add_workflow(conn, "하나")
    second = add_workflow(conn, "둘")
    third = add_workflow(conn, "셋")

    await scheduler._run_round()

    assert calls == [first, second, third]
    assert clock.slept == [49 * 60, 49 * 60]
    assert clock.now == kst(10, 11)


async def test_중지된_곳과_오늘_이미_돈_곳은_빠진다(
    scheduler: WorkflowScheduler, conn: sqlite3.Connection, calls: list[int]
) -> None:
    """한 바퀴 도중 서버가 다시 떠도 처음부터 다시 돌지 않는다."""
    done_today = add_workflow(conn, "오늘 돎")
    add_workflow(conn, "중지", status="paused")
    ran_yesterday = add_workflow(conn, "어제 돎")
    manual_today = add_workflow(conn, "오늘 손으로")
    add_run(conn, done_today, kst(8, 31), "schedule")
    add_run(conn, ran_yesterday, kst(8, 31, day=27), "schedule")
    add_run(conn, manual_today, kst(8, 29), "manual")

    await scheduler._run_round()

    assert calls == [ran_yesterday, manual_today]


async def test_차례가_오기_전에_중지되면_건너뛴다(
    scheduler: WorkflowScheduler, conn: sqlite3.Connection, calls: list[int]
) -> None:
    first = add_workflow(conn, "하나")
    second = add_workflow(conn, "둘")

    original = scheduler._runner

    async def pause_next(workflow_id: int) -> None:
        await original(workflow_id)
        conn.execute("UPDATE workflows SET status = 'paused' WHERE id = ?", (second,))

    scheduler._runner = pause_next
    await scheduler._run_round()

    assert calls == [first]


async def test_한_곳이_죽어도_남은_곳은_돈다(
    scheduler: WorkflowScheduler, conn: sqlite3.Connection, calls: list[int]
) -> None:
    first = add_workflow(conn, "죽는 곳")
    second = add_workflow(conn, "도는 곳")
    original = scheduler._runner

    async def fail_first(workflow_id: int) -> None:
        if workflow_id == first:
            raise RuntimeError("boom")
        await original(workflow_id)

    scheduler._runner = fail_first
    await scheduler._run_round()

    assert calls == [second]


async def test_시간이_지나_있으면_쉬지_않고_이어_돈다(
    scheduler: WorkflowScheduler, conn: sqlite3.Connection, calls: list[int], clock: Clock
) -> None:
    clock.now = kst(11, 30)
    add_workflow(conn, "하나")
    add_workflow(conn, "둘")

    await scheduler._run_round()

    assert len(calls) == 2
    assert clock.slept == []


def test_시간_안에_늦게_뜨면_바로_시작한다(
    scheduler: WorkflowScheduler, conn: sqlite3.Connection, clock: Clock
) -> None:
    clock.now = kst(9, 0)
    add_workflow(conn, "하나")
    scheduler.sync(conn)

    assert scheduler.catch_up(conn)
    assert scheduler.scheduler.get_job(DAILY_JOB_ID).next_run_time == kst(9, 0)


@pytest.mark.parametrize("now", [kst(8, 0), kst(11, 0), kst(15, 0)])
def test_시간_밖이면_기다린다(
    scheduler: WorkflowScheduler, conn: sqlite3.Connection, clock: Clock, now: datetime
) -> None:
    """개발 서버를 오후에 띄울 때마다 전 사이트를 돌지 않는다."""
    clock.now = now
    add_workflow(conn, "하나")
    scheduler.sync(conn)

    assert not scheduler.catch_up(conn)


def test_오늘_다_돌았으면_늦게_떠도_다시_돌지_않는다(
    scheduler: WorkflowScheduler, conn: sqlite3.Connection, clock: Clock
) -> None:
    clock.now = kst(9, 0)
    workflow_id = add_workflow(conn, "하나")
    add_run(conn, workflow_id, kst(8, 30), "schedule")
    scheduler.sync(conn)

    assert not scheduler.catch_up(conn)


def test_다음_실행_예정은_순번만큼_간격을_더한다(
    scheduler: WorkflowScheduler, conn: sqlite3.Connection
) -> None:
    first = add_workflow(conn, "하나")
    second = add_workflow(conn, "둘")
    add_workflow(conn, "중지", status="paused")
    scheduler.sync(conn)
    scheduler.scheduler.get_job(DAILY_JOB_ID).next_run_time = kst(8, 30, day=29)

    planned = scheduler.next_run_times(conn)

    assert planned == {first: kst(8, 30, day=29), second: kst(9, 45, day=29)}


def test_우리_잡이_아닌_id_는_건드리지_않는다(
    scheduler: WorkflowScheduler, conn: sqlite3.Connection
) -> None:
    async def unrelated() -> None:
        return None

    scheduler.scheduler.add_job(unrelated, "interval", minutes=5, id="cleanup:snapshots")
    add_workflow(conn, "워크플로우")

    scheduler.sync(conn)

    assert scheduler.scheduler.get_job("cleanup:snapshots") is not None
