"""동시성 상한과 중복 실행 스킵 테스트.

실사이트에 나가지 않는다. 문을 지나는 것은 실제 크롤링이 아니라 언제 들어가고 언제 나갔는지만
기록하는 코루틴이다 — 확인하려는 것은 "동시에 몇 개가 안에 있었나" 하나다.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import sqlite3
from datetime import UTC, datetime

import pytest
from apscheduler.events import EVENT_JOB_MAX_INSTANCES, JobSubmissionEvent

from app import db, settings
from app.scheduler import (
    DAILY_JOB_ID,
    RunGate,
    _configured_limit,
    _log_skipped_tick,
    get_gate,
)


class Tracker:
    """문 안에 있던 최대 인원을 센다."""

    def __init__(self) -> None:
        self.inside = 0
        self.peak = 0
        self.done = 0

    async def work(self, gate: RunGate, seconds: float = 0.05) -> None:
        async with gate.slot():
            self.inside += 1
            self.peak = max(self.peak, self.inside)
            await asyncio.sleep(seconds)
            self.inside -= 1
            self.done += 1


async def test_상한만큼만_동시에_들어간다() -> None:
    tracker = Tracker()
    gate = RunGate(lambda: 2)

    await asyncio.gather(*(tracker.work(gate) for _ in range(5)))

    assert tracker.peak == 2
    assert tracker.done == 5
    assert gate.active == 0


async def test_상한이_1_이면_한_번에_하나씩_돈다() -> None:
    tracker = Tracker()
    gate = RunGate(lambda: 1)

    await asyncio.gather(*(tracker.work(gate) for _ in range(4)))

    assert tracker.peak == 1
    assert tracker.done == 4


async def test_상한을_올리면_다음_획득부터_반영된다() -> None:
    """문을 다시 만들지 않는다. 같은 문이 바뀐 값을 읽는다."""
    limit = 1
    gate = RunGate(lambda: limit)
    first = Tracker()
    await asyncio.gather(*(first.work(gate) for _ in range(3)))
    assert first.peak == 1

    limit = 3
    second = Tracker()
    await asyncio.gather(*(second.work(gate) for _ in range(3)))

    assert second.peak == 3
    assert gate.limit() == 3


async def test_상한을_내려도_진행_중인_실행은_끊기지_않는다() -> None:
    limit = 3
    gate = RunGate(lambda: limit)
    tracker = Tracker()

    running = [asyncio.create_task(tracker.work(gate, seconds=0.2)) for _ in range(3)]
    await asyncio.sleep(0.05)
    assert tracker.inside == 3

    limit = 1
    # 상한 아래로 내려갈 때까지 새 실행은 들어가지 못한다
    waiting = asyncio.create_task(tracker.work(gate, seconds=0.01))
    await asyncio.sleep(0.05)
    assert tracker.inside == 3
    assert not waiting.done()

    await asyncio.gather(*running, waiting)
    assert tracker.done == 4
    assert gate.active == 0


async def test_기다린_사실이_로그로_남는다(caplog: pytest.LogCaptureFixture) -> None:
    tracker = Tracker()
    gate = RunGate(lambda: 1)

    with caplog.at_level(logging.INFO, logger="app.scheduler"):
        await asyncio.gather(*(tracker.work(gate) for _ in range(2)))

    assert any("동시 실행 상한" in record.message for record in caplog.records)


def skip_event(identifier: str) -> JobSubmissionEvent:
    """APScheduler 가 상한에 걸린 제출을 버릴 때 보내는 것과 같은 사건.

    실행이 아니라 제출이 막힌 것이라 `JobSubmissionEvent` 다. 여기서 `JobExecutionEvent` 를
    쓰면 테스트만 통과하고 운영에서는 리스너가 AttributeError 로 죽는다.
    """
    return JobSubmissionEvent(
        code=EVENT_JOB_MAX_INSTANCES,
        job_id=identifier,
        jobstore="default",
        scheduled_run_times=[datetime.now(UTC)],
    )


def test_건너뛴_매일_바퀴가_남는다(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """앞 바퀴가 끝나지 않아 APScheduler 가 이번 바퀴를 버린 경우다."""
    event = skip_event(DAILY_JOB_ID)

    with caplog.at_level(logging.WARNING, logger="app.scheduler"):
        _log_skipped_tick(event)

    assert len(caplog.records) == 1
    assert "매일 수집" in caplog.records[0].message
    assert "건너뛴다" in caplog.records[0].message


def test_상한은_app_settings_에서_읽는다(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`.env` 가 아니라 DB 가 진실이다. 재시작 없이 다음 획득부터 새 값이 쓰인다."""
    # 전역 문은 이 테스트 안에서만 만들어지고 끝나면 원래대로 돌아간다
    monkeypatch.setattr("app.scheduler._gate", None)
    path = tmp_path / "jobs.db"
    connect = db.connect
    setup = connect(path)
    db.migrate_up(setup)
    settings.write_int(setup, settings.MAX_CONCURRENT_RUNS, 2)
    setup.close()

    def connect_tmp(*args: object, **kwargs: object) -> sqlite3.Connection:
        return connect(path)

    monkeypatch.setattr("app.scheduler.db.connect", connect_tmp)
    assert _configured_limit() == 2

    changed = connect(path)
    settings.write_int(changed, settings.MAX_CONCURRENT_RUNS, 5)
    changed.close()

    assert _configured_limit() == 5
    # 전역 문 하나. 상한은 모두가 같은 문을 지날 때만 사실이다
    assert get_gate() is get_gate()
    assert get_gate().limit() == 5


def test_우리_잡이_아닌_스킵은_적지_않는다(caplog: pytest.LogCaptureFixture) -> None:
    event = skip_event("cleanup:snapshots")

    with caplog.at_level(logging.WARNING, logger="app.scheduler"):
        _log_skipped_tick(event)

    assert caplog.records == []
