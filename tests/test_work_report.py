"""작업 결과 슬랙 보고 테스트.

확인하는 것은 넷이다. 웹훅이 비면 보내지 않는지, 보고가 실패해도 작업으로 예외가 가지 않는지, 한
바퀴 끝에 수집과 분류·전송 두 통이 순서대로 나가는지, 수집 직후 분류는 따로 보내지 않는지.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import db
from app.job_analysis import schedule as job_analysis_schedule
from app.notify import daily_report, work_report
from app.scheduler import WorkflowScheduler
from app.side import runner as side_runner
from app.side import runs as side_runs
from app.side import store as side_store

WEBHOOK = "https://hooks.slack.com/services/T000/B000/secret"


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    for name in ("가", "나"):
        cursor = connection.execute(
            "INSERT INTO crawlers (name, list_url, status) VALUES (?, ?, 'promoted')",
            (name, f"https://{name}.test/"),
        )
        connection.execute(
            "INSERT INTO workflows (crawler_id, name) VALUES (?, ?)", (cursor.lastrowid, name)
        )
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """웹훅을 채우고 보낸 글을 모은다. 실제 슬랙으로는 나가지 않는다."""
    settings = SimpleNamespace(daily_report_slack_webhook=WEBHOOK, display_timezone="Asia/Seoul")
    monkeypatch.setattr(work_report, "get_settings", lambda: settings)
    texts: list[str] = []

    async def send(webhook: str, text: str, *, transport: object = None) -> object:
        texts.append(text)
        return SimpleNamespace(ok=True, detail="보냈다")

    monkeypatch.setattr(work_report, "send", send)
    return texts


def add_run(
    conn: sqlite3.Connection, workflow_id: int, status: str, *, new: int = 0, skipped: int = 0
) -> None:
    conn.execute(
        """
        INSERT INTO crawl_runs (workflow_id, trigger, status, new_count, skipped_count, finished_at)
        VALUES (?, 'schedule', ?, ?, ?, datetime('now'))
        """,
        (workflow_id, status, new, skipped),
    )


def test_제목은_날짜와_결과를_적는다() -> None:
    day = datetime(2026, 11, 2)

    assert work_report.build_text(day, "공고 분석", ["대상 3건"]) == (
        "*11/2 공고 분석 완료*\n• 대상 3건"
    )
    assert "공고 분석 실패" in work_report.build_text(day, "공고 분석", [], ok=False)


async def test_웹훅이_비면_보내지_않는다() -> None:
    assert await work_report.report("공고 분석", ["대상 3건"]) is None


async def test_슬랙이_거절해도_예외가_나가지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(daily_report_slack_webhook=WEBHOOK, display_timezone="Asia/Seoul")
    monkeypatch.setattr(work_report, "get_settings", lambda: settings)

    result = await work_report.report(
        "공고 분석",
        ["대상 3건"],
        transport=httpx.MockTransport(lambda _: httpx.Response(403)),
    )

    assert result is not None and not result.ok


async def test_보내다_터져도_예외가_나가지_않는다(
    monkeypatch: pytest.MonkeyPatch, sent: list[str]
) -> None:
    async def explode(*_: object, **__: object) -> None:
        raise RuntimeError("터졌다")

    monkeypatch.setattr(work_report, "send", explode)

    assert await work_report.report("공고 분석", []) is None


def test_다시_돈_사이트는_마지막_실행으로_센다(conn: sqlite3.Connection) -> None:
    add_run(conn, 1, "failed")
    add_run(conn, 1, "success", new=3, skipped=2)
    add_run(conn, 2, "failed")

    day = daily_report.crawl_day(conn)

    assert day.sites == 2
    assert day.failed_names == ("나",)
    assert day.new == 3
    assert day.skipped == 2


def test_어제_실행과_수동_실행은_세지_않는다(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO crawl_runs (workflow_id, trigger, status, new_count, started_at)
        VALUES (1, 'schedule', 'success', 9, datetime('now', '-2 days'))
        """
    )
    conn.execute(
        "INSERT INTO crawl_runs (workflow_id, trigger, status, new_count)"
        " VALUES (1, 'manual', 'success', 9)"
    )

    assert daily_report.crawl_day(conn).new == 0


def test_실패_사이트는_열_곳까지_적고_나머지는_센다() -> None:
    names = tuple(f"곳{index}" for index in range(12))

    lines = daily_report.crawl_lines(
        daily_report.CrawlDay(sites=12, failed_names=names, started_at="08:30", finished_at="11:00")
    )

    assert "성공 0, 실패 12" in lines[1]
    assert lines[2].endswith("곳9 외 2곳")


async def test_한_바퀴_끝에_두_통을_차례로_보내고_끝났다고_적는다(
    conn: sqlite3.Connection, sent: list[str]
) -> None:
    add_run(conn, 1, "success", new=2)

    assert not daily_report.round_finished_today(conn)
    await daily_report.finish_round(conn)

    assert len(sent) == 2
    assert "공고 수집 완료" in sent[0]
    assert "새 공고 2건" in sent[0]
    assert "공고 분류·전송 완료" in sent[1]
    assert daily_report.round_finished_today(conn)


async def test_앞선_분류가_돌면_기다렸다가_남은_공고를_거둔다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    side_store.create(
        conn, kind="classify", name="수집 직후 분류", status="active", trigger_kind="after_crawl"
    )
    states = ["돌고 있다", None]
    monkeypatch.setattr(daily_report, "classify_running", lambda _: states.pop(0))
    ran: list[str] = []
    monkeypatch.setattr(
        daily_report,
        "run_now",
        lambda _conn, _id, *, trigger: ran.append(trigger),
    )
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    note = await daily_report._drain(conn, sleep=sleep)

    assert note == ""
    assert slept == [daily_report.DRAIN_POLL_SECONDS]
    assert ran == [side_runner.AFTER_CRAWL]


async def test_분류가_끝나지_않으면_사유를_남기고_넘어간다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daily_report, "classify_running", lambda _: "돌고 있다")
    ran: list[int] = []
    monkeypatch.setattr(daily_report, "run_now", lambda *_, **__: ran.append(1))

    async def sleep(_: float) -> None:
        return None

    note = await daily_report._drain(conn, sleep=sleep, wait_seconds=10)

    assert "건너뛰었다" in note
    assert ran == []


async def test_한_바퀴가_끝나지_않은_날은_현황에_적는다(conn: sqlite3.Connection) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    transport = httpx.MockTransport(handle)
    await daily_report.report(conn, WEBHOOK, transport=transport)
    assert daily_report.UNFINISHED in json.loads(seen[0].content)["text"]

    daily_report._mark_finished(conn)
    await daily_report.report(conn, WEBHOOK, transport=transport)
    assert daily_report.UNFINISHED not in json.loads(seen[1].content)["text"]


def _run(trigger: str, status: str) -> side_runs.SideRun:
    now = datetime.now(UTC) - timedelta(minutes=1)
    return side_runs.SideRun(
        id=1,
        side_workflow_id=1,
        trigger=trigger,
        started_at=now.isoformat(),
        finished_at=now.isoformat(),
        status=status,
        target_count=3,
        processed_count=3,
        failed_count=0,
        note=None,
        error_message=None,
    )


def test_수집_직후_분류와_건너뛴_실행은_따로_보내지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        side_runner.work_report, "report_sync", lambda name, *_, **__: calls.append(name)
    )

    side_runner._report(_run(side_runner.AFTER_CRAWL, side_runs.SUCCESS))
    side_runner._report(_run(side_runner.SCHEDULE, side_runs.SKIPPED))
    side_runner._report(_run(side_runner.SCHEDULE, side_runs.SUCCESS))

    assert calls == ["분류"]


async def test_분석은_한_바퀴가_끝나기를_기다린다(monkeypatch: pytest.MonkeyPatch) -> None:
    states = [True, True, False]

    class Scheduler:
        @property
        def round_busy(self) -> bool:
            return states.pop(0)

    monkeypatch.setattr(job_analysis_schedule, "get_scheduler", Scheduler)
    monkeypatch.setattr(job_analysis_schedule, "ROUND_POLL_SECONDS", 0)

    await job_analysis_schedule.wait_for_round(datetime.now(UTC) + timedelta(hours=1))

    assert states == []


async def test_멈춤_시각이_지나면_더_기다리지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        job_analysis_schedule, "get_scheduler", lambda: SimpleNamespace(round_busy=True)
    )

    await job_analysis_schedule.wait_for_round(datetime.now(UTC) - timedelta(seconds=1))


async def test_한_바퀴_끝에_마무리를_부르고_그동안은_돌고_있다고_말한다(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    location = tmp_path / "round.db"
    connect = db.connect
    monkeypatch.setattr("app.scheduler.db.connect", lambda *_, **__: connect(location))
    connection = connect(location)
    db.migrate_up(connection)
    cursor = connection.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES ('곳', 'https://a.test/', 'promoted')"
    )
    connection.execute(
        "INSERT INTO workflows (crawler_id, name, status) VALUES (?, '곳', 'active')",
        (cursor.lastrowid,),
    )
    connection.close()
    busy: list[bool] = []

    async def run(_: int) -> None:
        return None

    async def finish() -> None:
        busy.append(instance.round_busy)

    now = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)
    instance = WorkflowScheduler(
        scheduler=AsyncIOScheduler(timezone="UTC"),
        runner=run,
        clock=lambda: now,
        finisher=finish,
    )

    await instance._run_round()

    assert busy == [True]
    assert instance.round_busy is False
