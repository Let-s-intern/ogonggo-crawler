"""상시 채용 공고를 목록에서 본 기록과 마감 판정 (`app/crawler/sightings.py`).

| 확인 | 깨지면 |
|---|---|
| 사흘 연달아 안 보이면 마감으로 본다 | 내려간 공고가 계속 모집 중이다 |
| 안 보이다가 다시 보이면 횟수와 판정을 되돌린다 | 잠깐 빠진 공고가 마감으로 남는다 |
| 기간 채용은 보지 않는다 | 마감일이 있는 공고를 목록으로 또 판정한다 |
| 믿을 수 없는 실행(꺼진 사이트·수동·실패·목록 실패·첫 실행 상한)은 판정 안 함 | 무더기 마감 |
| 목록 수가 평소의 절반 미만이거나 견줄 실행이 모자라면 판정 안 함 | 깨진 날 마감 |
| 목록 수를 실행 기록에 남긴다 | 평소 목록 수를 알 수 없다 |
| 0063 이 greetinghr 만 켠다 | 첫 쪽만 읽는 사이트가 켜진 채 시작한다 |
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from app import db
from app.crawler import sightings
from app.crawler.runner import MANUAL, SCHEDULE, RunResult


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (id, name, list_url, status, list_complete)"
        " VALUES (1, 'X', 'https://x', 'promoted', 1)"
    )
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, 'X')")
    try:
        yield connection
    finally:
        connection.close()


def add(conn: sqlite3.Connection, raw_id: int, recruitment_type: str = "ALWAYS_OPEN") -> str:
    url = f"https://x/{raw_id}"
    conn.execute(
        "INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)"
        " VALUES (?, 1, ?, ?, ?)",
        (raw_id, url, json.dumps({"title": f"공고 {raw_id}"}), f"h{raw_id}"),
    )
    conn.execute(
        "INSERT INTO normalized_jobs (raw_job_id, source_url, recruitment_type) VALUES (?, ?, ?)",
        (raw_id, url, recruitment_type),
    )
    return url


def past_runs(conn: sqlite3.Connection, matched: int = 10, count: int = 3) -> None:
    for _ in range(count):
        conn.execute(
            "INSERT INTO crawl_runs (workflow_id, status, trigger, matched)"
            " VALUES (1, 'success', 'schedule', ?)",
            (matched,),
        )


def run(
    conn: sqlite3.Connection,
    links: list[str],
    *,
    matched: int = 10,
    status: str = "success",
    trigger: str = SCHEDULE,
    list_misses: int = 0,
    capped: bool = False,
) -> sightings.SightingResult:
    cursor = conn.execute(
        "INSERT INTO crawl_runs (workflow_id, status, trigger, matched) VALUES (1, ?, ?, ?)",
        (status, trigger, matched),
    )
    result = RunResult(
        run_id=int(cursor.lastrowid or 0),
        status=status,
        matched=matched,
        seen_links=links,
        list_misses=list_misses,
    )
    return sightings.record(conn, 1, result, trigger=trigger, capped=capped)


def state(conn: sqlite3.Connection, url: str) -> tuple[int, bool]:
    row = conn.execute(
        "SELECT missed_runs, closed_at FROM job_sightings WHERE workflow_id = 1 AND source_url = ?",
        (url,),
    ).fetchone()
    return int(row["missed_runs"]), row["closed_at"] is not None


def test_사흘_연달아_안_보이면_마감으로_본다(conn: sqlite3.Connection) -> None:
    kept, gone = add(conn, 1), add(conn, 2)
    past_runs(conn)

    for day in range(1, 3):
        outcome = run(conn, [kept])
        assert state(conn, gone) == (day, False)
        assert outcome.closed == 0

    outcome = run(conn, [kept])
    assert state(conn, gone) == (3, True)
    assert state(conn, kept) == (0, False)
    assert (outcome.seen, outcome.missed, outcome.closed) == (1, 1, 1)


def test_다시_보이면_횟수와_판정을_되돌린다(conn: sqlite3.Connection) -> None:
    url = add(conn, 1)
    past_runs(conn)
    for _ in range(3):
        run(conn, [])
    assert state(conn, url) == (3, True)

    outcome = run(conn, [url])

    assert state(conn, url) == (0, False)
    assert outcome.reopened == 1


def test_기간_채용은_보지_않는다(conn: sqlite3.Connection) -> None:
    add(conn, 1, recruitment_type="PERIOD")
    past_runs(conn)

    outcome = run(conn, [])

    assert outcome.missed == 0
    assert conn.execute("SELECT count(*) FROM job_sightings").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"trigger": MANUAL}, "매일 수집이 아니다"),
        ({"status": "failed"}, "성공하지 않았다"),
        ({"list_misses": 1}, "읽지 못했다"),
        ({"capped": True}, "첫 실행 상한"),
        ({"matched": 4}, "절반보다 적다"),
    ],
)
def test_믿을_수_없는_실행은_판정하지_않는다(
    conn: sqlite3.Connection, kwargs: dict[str, object], reason: str
) -> None:
    url = add(conn, 1)
    past_runs(conn)

    outcome = run(conn, [], **kwargs)  # type: ignore[arg-type]

    assert reason in outcome.reason
    assert conn.execute("SELECT count(*) FROM job_sightings").fetchone()[0] == 0
    assert url


def test_목록_전부_보임이_꺼진_사이트는_판정하지_않는다(conn: sqlite3.Connection) -> None:
    add(conn, 1)
    past_runs(conn)
    conn.execute("UPDATE crawlers SET list_complete = 0")

    outcome = run(conn, [])

    assert "켜지 않았다" in outcome.reason


def test_견줄_실행이_모자라면_판정하지_않는다(conn: sqlite3.Connection) -> None:
    add(conn, 1)
    past_runs(conn, count=2)

    outcome = run(conn, [])

    assert "2번뿐" in outcome.reason


def test_수동_실행과_실패한_실행의_목록_수는_평소에_세지_않는다(conn: sqlite3.Connection) -> None:
    add(conn, 1)
    past_runs(conn, count=2)
    conn.execute(
        "INSERT INTO crawl_runs (workflow_id, status, trigger, matched)"
        " VALUES (1, 'success', 'manual', 10), (1, 'failed', 'schedule', 10)"
    )

    outcome = run(conn, [])

    assert "2번뿐" in outcome.reason


def test_0063_은_greetinghr_만_켠다(tmp_path: pathlib.Path) -> None:
    connection = db.connect(tmp_path / "jobs.db")
    try:
        db.migrate_up(connection)
        applied = db.applied_versions(connection)
        db.migrate_down(connection, steps=len(applied) - applied.index("0063"))
        connection.executemany(
            "INSERT INTO crawlers (name, list_url, status) VALUES (?, ?, 'promoted')",
            [
                ("예림", "https://yerim.career.greetinghr.com/ko/apply"),
                ("HD현대", "https://hd.recruiter.co.kr/career/job"),
            ],
        )
        db.migrate_up(connection)

        rows = dict(connection.execute("SELECT name, list_complete FROM crawlers").fetchall())
    finally:
        connection.close()

    assert rows == {"예림": 1, "HD현대": 0}


async def test_매일_수집이_목록_수를_남기고_사라진_상시_채용을_센다(tmp_path: pathlib.Path) -> None:
    """러너를 그대로 돌린다. 목록 수가 실행 기록에 남고, 목록에 없는 상시 채용 공고가 세진다."""
    from app import settings
    from app.crawler.runner import run_workflow
    from tests.test_auto_stop import LIST_URL, WORKING, stub_fetcher

    connection = db.connect(tmp_path / "jobs.db")
    try:
        db.migrate_up(connection)
        settings.write_int(connection, settings.FIRST_RUN_LIMIT, 0)
        connection.execute(
            "INSERT INTO crawlers (id, name, list_url, selectors_json, status, list_complete)"
            " VALUES (1, 'python.org', ?, ?, 'promoted', 1)",
            (LIST_URL, json.dumps(WORKING)),
        )
        connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, 'X')")
        gone = add(connection, 9001)

        first = await run_workflow(connection, 1, trigger=SCHEDULE, fetcher=stub_fetcher())
        stored = connection.execute(
            "SELECT matched FROM crawl_runs WHERE id = ?", (first.run_id,)
        ).fetchone()["matched"]
        assert stored == first.matched > 0
        assert len(first.seen_links) == first.matched

        past_runs(connection, matched=first.matched)
        await run_workflow(connection, 1, trigger=SCHEDULE, fetcher=stub_fetcher())
    finally:
        missed = state(connection, gone)
        connection.close()

    assert missed == (1, False)
