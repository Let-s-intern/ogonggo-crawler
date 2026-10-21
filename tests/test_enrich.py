"""부실한 공고 다시 읽기 (`app/crawler/enrich.py`).

사이트는 `httpx.MockTransport` 이고 이미지 읽기는 대역이다. 브라우저는 띄우지 않는다 — 정적 HTML 에
글자가 충분한 페이지만 쓴다.

| 확인 | 깨지면 |
|---|---|
| 본문이 짧은 공고를 페이지 전체로 다시 읽어 갈아 끼운다 | 운영자가 원문을 붙여 넣어야 한다 |
| 갈아 끼우기 전 값은 이력에 남고, 본문 밖 칸은 그대로다 | 셀렉터가 읽은 제목·마감일이 사라진다 |
| 다시 읽어도 더 나은 본문이 없으면 그대로 둔다 | 짧은 본문이 다른 짧은 본문으로 바뀐다 |
| 공고마다 한 번이고, 직접 추가한 공고는 보지 않는다 | 수집할 때마다 같은 페이지를 연다 |
| 매일 수집은 이번에 담은 짧은 공고만 연다 | 한 실행이 쌓인 공고를 전부 연다 |
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from app import db
from app.crawler import enrich
from app.crawler.manual import MANUAL_KIND
from tests.test_manual_posting import BODY, PAGE, URL, FakeReader, site

SHORT_PAGE = (
    "<html><body><main><h1>공고</h1><p>자세한 내용은 첨부를 보세요</p></main></body></html>"
)


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (id, name, list_url, status)"
        " VALUES (1, '동원', 'https://x', 'promoted')"
    )
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, '동원')")
    try:
        yield connection
    finally:
        connection.close()


def add(
    conn: sqlite3.Connection, raw_id: int, url: str = URL, body: str = "지원하기", workflow: int = 1
) -> int:
    record = {
        "source_url": url,
        "title": "[동원건설산업] 전기 견적",
        "body": body,
        "recruitment_end_at": "2026-11-30",
        "company_name": "동원건설산업",
    }
    conn.execute(
        "INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)"
        " VALUES (?, ?, ?, ?, ?)",
        (raw_id, workflow, url, json.dumps(record, ensure_ascii=False), f"h{raw_id}"),
    )
    conn.execute(
        "INSERT INTO normalized_jobs (raw_job_id, source_url, title, body) VALUES (?, ?, ?, ?)",
        (raw_id, url, record["title"], body),
    )
    return raw_id


def run(conn: sqlite3.Connection, ids: list[int], pages: dict[str, str]) -> list[enrich.Enriched]:
    return asyncio.run(enrich.enrich_ids(conn, ids, fetcher=site(pages), reader=FakeReader()))


def raw(conn: sqlite3.Connection, raw_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT raw_data_json FROM raw_jobs WHERE id = ?", (raw_id,)).fetchone()
    return dict(json.loads(row[0]))


def test_a_short_posting_is_read_again_from_the_whole_page(conn: sqlite3.Connection) -> None:
    raw_id = add(conn, 1)

    [result] = run(conn, enrich.short_ids(conn), {URL: PAGE})

    assert result.outcome == enrich.ENRICHED
    assert BODY.split()[0] in raw(conn, raw_id)["body"]
    assert "body_html" in raw(conn, raw_id)
    body = conn.execute("SELECT body FROM normalized_jobs WHERE raw_job_id = ?", (raw_id,))
    assert len(body.fetchone()[0]) > enrich.SHORT_BODY_CHARS


def test_the_old_record_goes_to_history_and_other_fields_stay(conn: sqlite3.Connection) -> None:
    raw_id = add(conn, 1)

    run(conn, [raw_id], {URL: PAGE})

    kept = raw(conn, raw_id)
    assert (kept["title"], kept["recruitment_end_at"]) == ("[동원건설산업] 전기 견적", "2026-11-30")
    history = conn.execute("SELECT raw_data_json FROM raw_job_history WHERE raw_job_id = ?", (1,))
    assert json.loads(history.fetchone()[0])["body"] == "지원하기"


def test_nothing_better_leaves_the_posting_alone(conn: sqlite3.Connection) -> None:
    raw_id = add(conn, 1)

    [result] = run(conn, [raw_id], {URL: SHORT_PAGE})

    assert result.outcome == enrich.UNCHANGED
    assert raw(conn, raw_id)["body"] == "지원하기"
    assert conn.execute("SELECT count(*) FROM raw_job_history").fetchone()[0] == 0


def test_a_page_that_cannot_be_opened_is_recorded(conn: sqlite3.Connection) -> None:
    raw_id = add(conn, 1)

    [result] = run(conn, [raw_id], {})

    assert result.outcome == enrich.ERROR
    assert raw(conn, raw_id)["body"] == "지원하기"


def test_each_posting_is_read_again_once(conn: sqlite3.Connection) -> None:
    raw_id = add(conn, 1)
    run(conn, [raw_id], {URL: SHORT_PAGE})

    assert enrich.short_ids(conn) == []
    outcome = conn.execute("SELECT outcome, before_chars FROM posting_enrichments").fetchone()
    assert tuple(outcome) == (enrich.UNCHANGED, len("지원하기"))


def test_long_and_hand_added_postings_are_not_picked(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO workflows (id, crawler_id, name, kind) VALUES (2, 1, '직접 추가', ?)",
        (MANUAL_KIND,),
    )
    add(conn, 1, body="본문 " * 200)
    add(conn, 2, url=f"{URL}/2", workflow=2)
    short = add(conn, 3, url=f"{URL}/3")

    assert enrich.short_ids(conn) == [short]
    assert enrich.short_ids(conn, [1, 2]) == []


def test_after_a_run_only_the_postings_it_stored_are_read(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(conn, 1)
    add(conn, 2, url=f"{URL}/2")
    opened: list[int] = []

    async def enrich_ids(conn: sqlite3.Connection, ids: list[int], **_: Any) -> list[Any]:
        opened.extend(ids)
        return []

    monkeypatch.setattr(enrich, "enrich_ids", enrich_ids)
    monkeypatch.setattr(enrich, "MAX_PER_RUN", 1)

    asyncio.run(enrich.after_run(conn, 9, [2, 1]))

    assert opened == [1]
