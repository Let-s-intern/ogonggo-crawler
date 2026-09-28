"""부가 워크플로우의 스케줄러 등록 테스트.

`tests/test_scheduler.py` 와 같은 자리이고 대상만 다르다. 확인하는 것은 "표가 이러면 잡이
이렇게 된다" 하나이고, 주기가 되면 실제로 도는지는 APScheduler 의 책임이다.

모델에도 실사이트에도 나가지 않는다. 잡이 부르는 실행 함수는 id 만 받아 적는 스텁이다.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from apscheduler.events import EVENT_JOB_MAX_INSTANCES, JobSubmissionEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import db, settings
from app.scheduler import (
    DAILY_JOB_ID,
    SIDE_SCHEDULE,
    WorkflowScheduler,
    _log_skipped_tick,
    get_gate,
    side_job_id,
    side_workflow_id_of,
)
from app.side import store


@pytest.fixture
def scheduler() -> Iterator[WorkflowScheduler]:
    async def nothing(_: int) -> None:
        return None

    # 시작하지 않는다. 잡은 pending 으로 쌓이고 조회·갱신·제거는 그대로 동작한다
    instance = WorkflowScheduler(scheduler=AsyncIOScheduler(timezone="UTC"), runner=nothing)
    try:
        yield instance
    finally:
        instance.shutdown()


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
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
def calls() -> list[str]:
    return []


@pytest.fixture
def synced(calls: list[str]) -> Iterator[WorkflowScheduler]:
    """두 종류의 실행 함수를 갈아끼운 스케줄러. 무엇이 불렸는지가 목록에 남는다."""

    async def crawl(workflow_id: int) -> None:
        calls.append(f"crawl:{workflow_id}")

    async def side(side_workflow_id: int) -> None:
        calls.append(f"side:{side_workflow_id}")

    instance = WorkflowScheduler(
        scheduler=AsyncIOScheduler(timezone="UTC"), runner=crawl, side_runner=side
    )
    try:
        yield instance
    finally:
        instance.shutdown()


def add_workflow(conn: sqlite3.Connection, name: str, minutes: int) -> int:
    cursor = conn.execute(
        """
        INSERT INTO workflows (crawler_id, name, interval_minutes, status)
        VALUES (1, ?, ?, 'active')
        """,
        (name, minutes),
    )
    return int(cursor.lastrowid or 0)


def add_side(
    conn: sqlite3.Connection,
    name: str,
    minutes: int,
    *,
    status: str = store.ACTIVE,
    trigger_kind: str = store.INTERVAL,
) -> int:
    workflow = store.create(
        conn,
        kind=store.CLASSIFY,
        name=name,
        status=status,
        trigger_kind=trigger_kind,
        interval_minutes=minutes,
    )
    return workflow.id


def test_잡_id_는_서로의_것을_읽지_않는다() -> None:
    """앞머리가 종류를 가른다. 남의 잡은 부가 잡으로 읽히지 않는다."""
    assert side_workflow_id_of(side_job_id(1)) == 1

    assert side_workflow_id_of(DAILY_JOB_ID) is None
    assert side_workflow_id_of("cleanup:snapshots") is None
    # 앞머리가 맞아도 뒤가 숫자가 아니면 우리 잡이 아니다
    assert side_workflow_id_of("side:classify") is None


def test_주기로_도는_active_인_것만_등록한다(
    synced: WorkflowScheduler, conn: sqlite3.Connection
) -> None:
    """상태와 실행 시점 둘 다 맞아야 잡이 된다.

    `after_crawl` 과 `manual` 은 주기 값이 적혀 있어도 등록하지 않는다. 부르는 자리가 따로
    있고, 여기서 함께 등록하면 운영자가 고르지 않은 주기로도 돈다.
    """
    running = add_side(conn, "도는 것", 60)
    stopped = add_side(conn, "멈춘 것", 60, status=store.PAUSED)
    after_crawl = add_side(conn, "크롤 뒤", 60, trigger_kind="after_crawl")
    manual = add_side(conn, "손으로", 60, trigger_kind="manual")

    report = synced.sync(conn)

    assert report.side_added == [running]
    assert synced.side_scheduled() == {running: 60}
    for excluded in (stopped, after_crawl, manual):
        assert excluded not in synced.side_scheduled()


def test_표를_바꾸면_잡_목록이_따라온다(
    synced: WorkflowScheduler, conn: sqlite3.Connection
) -> None:
    """주기 변경·중지·재개·삭제가 `sync()` 한 번으로 잡에 반영된다."""
    side_id = add_side(conn, "따라오는 것", 60)
    assert synced.sync(conn).side_added == [side_id]

    conn.execute("UPDATE side_workflows SET interval_minutes = 15 WHERE id = ?", (side_id,))
    report = synced.sync(conn)
    assert report.side_updated == [side_id]
    assert synced.side_scheduled() == {side_id: 15}

    conn.execute("UPDATE side_workflows SET status = 'paused' WHERE id = ?", (side_id,))
    report = synced.sync(conn)
    assert report.side_removed == [side_id]
    assert synced.side_scheduled() == {}

    conn.execute("UPDATE side_workflows SET status = 'active' WHERE id = ?", (side_id,))
    assert synced.sync(conn).side_added == [side_id]

    store.delete(conn, side_id)
    report = synced.sync(conn)
    assert report.side_removed == [side_id]
    assert synced.side_scheduled() == {}


def test_주기가_아니게_되면_잡이_사라진다(
    synced: WorkflowScheduler, conn: sqlite3.Connection
) -> None:
    """`interval` 에서 `manual` 로 바꾸면 active 여도 더 이상 깨우지 않는다."""
    side_id = add_side(conn, "손으로 바뀌는 것", 30)
    synced.sync(conn)

    conn.execute("UPDATE side_workflows SET trigger_kind = 'manual' WHERE id = ?", (side_id,))
    report = synced.sync(conn)

    assert report.side_removed == [side_id]
    assert synced.side_scheduled() == {}


def test_바뀐_것이_없으면_아무것도_하지_않는다(
    synced: WorkflowScheduler, conn: sqlite3.Connection
) -> None:
    add_side(conn, "그대로", 45)
    synced.sync(conn)

    assert not synced.sync(conn)


def test_부가_잡이_있어도_매일_잡은_그대로다(
    synced: WorkflowScheduler, conn: sqlite3.Connection
) -> None:
    """부가 잡을 멈춰도 매일 잡은 남고, 어느 표에도 없는 잡도 남는다."""

    async def unrelated() -> None:
        return None

    synced.scheduler.add_job(unrelated, "interval", minutes=5, id="cleanup:snapshots")
    add_workflow(conn, "크롤", 10)
    side_id = add_side(conn, "분류", 20)

    report = synced.sync(conn)
    assert report.daily == "08:30"
    assert report.side_added == [side_id]

    conn.execute("UPDATE side_workflows SET status = 'paused' WHERE id = ?", (side_id,))
    report = synced.sync(conn)

    assert report.side_removed == [side_id]
    assert report.daily == ""
    assert synced.scheduler.get_job(DAILY_JOB_ID) is not None
    assert synced.scheduler.get_job("cleanup:snapshots") is not None


async def test_부가_잡은_자기_실행_함수를_부른다(
    synced: WorkflowScheduler, conn: sqlite3.Connection, calls: list[str]
) -> None:
    """잡에 실린 것은 id 뿐이고, 부가 잡은 크롤 실행 함수로 가지 않는다."""
    side_id = add_side(conn, "분류", 20)
    synced.sync(conn)

    side = synced.scheduler.get_job(side_job_id(side_id))
    assert side.args == (side_id,)
    assert side.max_instances == 1

    await side.func(*side.args)

    assert calls == [f"side:{side_id}"]


async def test_부가_잡은_크롤_상한을_잡지_않는다(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """상한이 1이고 크롤이 그 하나를 쥐고 있어도 분류는 그 자리에서 돈다 (4.4.V).

    분류도 전달도 대상 사이트에 요청을 보내지 않는다. 크롤 슬롯을 하나 차지하면 지켜지는
    것은 없고 수집만 밀린다.

    기본 실행 경로(`_execute`, `_execute_side`)를 그대로 지난다. 실행 함수를 갈아끼우면
    문을 잡는 자리가 그 함수 바깥이라 이 결함이 잡히지 않는다.
    """
    path = tmp_path / "jobs.db"
    setup = db.connect(path)
    db.migrate_up(setup)
    settings.write_int(setup, settings.MAX_CONCURRENT_RUNS, 1)
    setup.close()

    connect = db.connect
    monkeypatch.setattr("app.scheduler.db.connect", lambda *_, **__: connect(path))
    # 전역 문은 이 테스트 안에서만 만들어진다
    monkeypatch.setattr("app.scheduler._gate", None)

    crawl_inside = asyncio.Event()
    release = asyncio.Event()
    side_triggers: list[str] = []

    async def crawl(conn: sqlite3.Connection, workflow_id: int, **kwargs: object) -> object:
        crawl_inside.set()
        await release.wait()
        return None

    def side(conn: sqlite3.Connection, side_workflow_id: int, **kwargs: object) -> object:
        side_triggers.append(str(kwargs.get("trigger")))
        return None

    monkeypatch.setattr("app.scheduler.run_workflow", crawl)
    monkeypatch.setattr("app.scheduler.run_side_now", side)

    instance = WorkflowScheduler(scheduler=AsyncIOScheduler(timezone="UTC"))
    try:
        running = asyncio.create_task(instance._execute(1))
        await asyncio.wait_for(crawl_inside.wait(), timeout=5)
        assert get_gate().active == 1

        # 문이 꽉 찬 상태에서 부가 잡을 돌린다. 문을 기다린다면 여기서 시간이 다 간다
        await asyncio.wait_for(instance._execute_side(1), timeout=5)

        assert side_triggers == [SIDE_SCHEDULE]
        # 크롤 하나만 문 안에 있다. 부가 잡은 지나갔지만 슬롯을 쓰지 않았다
        assert get_gate().active == 1
    finally:
        release.set()
        await running
        instance.shutdown()


def test_건너뛴_부가_tick_도_남는다(caplog: pytest.LogCaptureFixture) -> None:
    """겹침으로 버려진 차례는 실행 함수에 닿지 않는다. 여기서 적지 않으면 어디에도 안 남는다."""
    event = JobSubmissionEvent(
        code=EVENT_JOB_MAX_INSTANCES,
        job_id=side_job_id(7),
        jobstore="default",
        scheduled_run_times=[datetime.now(UTC)],
    )

    with caplog.at_level(logging.WARNING, logger="app.scheduler"):
        _log_skipped_tick(event)

    assert len(caplog.records) == 1
    assert "side workflow 7" in caplog.records[0].message
