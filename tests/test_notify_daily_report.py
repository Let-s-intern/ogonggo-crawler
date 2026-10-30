"""일일 공고 현황 슬랙 보내기 테스트.

확인하는 것은 셋이다. 건수가 공고 목록 화면과 같은 기준인지, 실패해도 예외와 웹훅 주소가 새지
않는지, 웹훅이 비면 잡이 걸리지 않는지. 망은 `httpx.MockTransport` 로 대신한다.
"""

from __future__ import annotations

import json
import logging
import pathlib
import sqlite3
from collections.abc import Iterator
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import db
from app.notify import daily_report

WEBHOOK = "https://hooks.slack.com/services/T000/B000/secret"


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status) "
        "VALUES ('site', 'https://a.test/', 'promoted')"
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '곳')")
    try:
        yield connection
    finally:
        connection.close()


def add_job(conn: sqlite3.Connection, raw_job_id: int, crawled_at: str) -> None:
    source_url = f"https://a.test/{raw_job_id}/"
    conn.execute(
        f"""
        INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash, crawled_at)
        VALUES (?, 1, ?, '{{}}', ?, {crawled_at})
        """,
        (raw_job_id, source_url, f"hash-{raw_job_id}"),
    )
    conn.execute(
        """
        INSERT INTO normalized_jobs (raw_job_id, company_name, title, body, source_url)
        VALUES (?, '회사', '공고', '본문', ?)
        """,
        (raw_job_id, source_url),
    )


def add_delivery(conn: sqlite3.Connection, name: str, status: str, sent_at: str) -> None:
    conn.execute(
        f"INSERT INTO spring_deliveries (source_url, status, sent_at) VALUES (?, ?, {sent_at})",
        (f"https://a.test/{name}/", status),
    )


def capture(status_code: int = 200) -> tuple[list[httpx.Request], httpx.MockTransport]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status_code, text="ok")

    return seen, httpx.MockTransport(handle)


def test_문구는_날짜와_두_건수를_적는다() -> None:
    text = daily_report.build_text(day=datetime(2026, 10, 30), crawled=12, sent=8)

    assert text == "*10/30 공고 현황*\n• 오늘 들어온 공고: 12건\n• 오공고로 보낸 공고: 8건"


async def test_오늘_들어온_공고와_오늘_보낸_공고만_센다(conn: sqlite3.Connection) -> None:
    add_job(conn, 1, "datetime('now')")
    add_job(conn, 2, "datetime('now')")
    add_job(conn, 3, "datetime('now', '-2 days')")
    add_delivery(conn, "sent-today", "sent", "datetime('now')")
    add_delivery(conn, "sent-before", "sent", "datetime('now', '-2 days')")
    add_delivery(conn, "failed", "failed", "NULL")
    seen, transport = capture()

    result = await daily_report.report(conn, WEBHOOK, transport=transport)

    assert result.ok
    text = json.loads(seen[0].content)["text"]
    assert "오늘 들어온 공고: 2건" in text
    assert "오공고로 보낸 공고: 1건" in text


async def test_0건인_날도_보낸다(conn: sqlite3.Connection) -> None:
    seen, transport = capture()

    await daily_report.report(conn, WEBHOOK, transport=transport)

    assert "오늘 들어온 공고: 0건" in json.loads(seen[0].content)["text"]


async def test_슬랙이_거절해도_예외가_나가지_않는다() -> None:
    _, transport = capture(status_code=403)

    result = await daily_report.send(WEBHOOK, "글", transport=transport)

    assert not result.ok
    assert result.status_code == 403


async def test_전송_실패에_웹훅_주소가_남지_않는다(caplog: pytest.LogCaptureFixture) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}")

    with caplog.at_level(logging.WARNING):
        result = await daily_report.send(WEBHOOK, "글", transport=httpx.MockTransport(fail))

    assert not result.ok
    assert "secret" not in result.detail
    assert "secret" not in caplog.text


def use_settings(monkeypatch: pytest.MonkeyPatch, webhook: str, time: str = "13:30") -> None:
    settings = SimpleNamespace(daily_report_slack_webhook=webhook, daily_report_time=time)
    monkeypatch.setattr(daily_report, "get_settings", lambda: settings)


def test_웹훅이_비면_잡을_걸지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    use_settings(monkeypatch, webhook="")
    scheduler = AsyncIOScheduler(timezone="UTC")

    assert daily_report.sync(scheduler) is False
    assert scheduler.get_job(daily_report.JOB_ID) is None


def test_웹훅이_있으면_설정한_시각에_잡을_건다(monkeypatch: pytest.MonkeyPatch) -> None:
    use_settings(monkeypatch, webhook=WEBHOOK, time="13:30")
    scheduler = AsyncIOScheduler(timezone="UTC")

    assert daily_report.sync(scheduler) is True
    job = scheduler.get_job(daily_report.JOB_ID)
    fields = {part.name: str(part) for part in job.trigger.fields}
    assert (fields["hour"], fields["minute"]) == ("13", "30")
    assert str(job.trigger.timezone) == "Asia/Seoul"
