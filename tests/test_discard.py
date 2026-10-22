"""공고 폐기 — 운영자가 지운 공고와 분류가 대상 직군 밖으로 정한 공고 (`app/discard.py`).

| 확인 | 깨지면 |
|---|---|
| 분류가 직군을 대상 밖으로 정한 공고만 고른다 | 대상 공고가 사라진다 |
| 나뉜 공고는 번호 하나라도 대상이면 남긴다 | 보낼 수 있는 직무까지 버린다 |
| 직군이 빈 공고, 보낸 공고, 직접 추가한 공고는 남긴다 | 분류 실패나 사람이 고른 공고가 사라진다 |
| 대상 직군을 모두 켜 두었으면 아무것도 고르지 않는다 | 전 직군을 받기로 했는데 버린다 |
| 버린 공고는 까닭과 함께 남고 다음 수집이 다시 담지 않는다 | 다음 날 다시 들어온다 |
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from app import db, discard
from app.crawler import runner, target_fields
from app.crawler.manual import MANUAL_KIND


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (id, name, list_url, status)"
        " VALUES (1, 'SK', 'https://x', 'promoted')"
    )
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, 'SK')")
    connection.execute(
        "INSERT INTO workflows (id, crawler_id, name, kind) VALUES (2, 1, '직접 추가', ?)",
        (MANUAL_KIND,),
    )
    try:
        yield connection
    finally:
        connection.close()


def add(
    conn: sqlite3.Connection,
    raw_id: int,
    *fields: str | None,
    workflow: int = 1,
    classified: bool = True,
) -> int:
    """공고 한 건. 직군을 하나 주면 하나로 남은 공고, 여럿 주면 직무마다 나뉜 공고다."""
    url = f"https://x/{raw_id}"
    conn.execute(
        "INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)"
        " VALUES (?, ?, ?, ?, ?)",
        (raw_id, workflow, url, json.dumps({"title": f"공고 {raw_id}"}), f"h{raw_id}"),
    )
    for part, field in enumerate(fields, start=1):
        conn.execute(
            "INSERT INTO normalized_jobs (raw_job_id, part, source_url, job_field)"
            " VALUES (?, ?, ?, ?)",
            (raw_id, part, url if part == 1 else f"{url}#{part}", field),
        )
        if classified:
            conn.execute(
                "INSERT INTO job_classifications (raw_job_id, part, model) VALUES (?, ?, 'm')",
                (raw_id, part),
            )
    return raw_id


def test_분류가_대상_직군_밖으로_정한_공고만_고른다(conn: sqlite3.Connection) -> None:
    off = add(conn, 1, "엔지니어링·R&D")
    add(conn, 2, "IT·개발")
    add(conn, 3, "엔지니어링·R&D", "IT·개발")
    off_split = add(conn, 4, "엔지니어링·R&D", "디자인")
    add(conn, 5, None)
    add(conn, 6, "엔지니어링·R&D", classified=False)
    add(conn, 7, "엔지니어링·R&D", workflow=2)
    add(conn, 8, "엔지니어링·R&D")
    conn.execute(
        "INSERT INTO spring_deliveries (source_url, status) VALUES ('https://x/8', 'sent')"
    )

    assert discard.off_target_ids(conn) == [off, off_split]
    assert discard.off_target_ids(conn, [1, 2, 3]) == [off]


def test_대상_직군을_모두_켜_두면_고르지_않는다(conn: sqlite3.Connection) -> None:
    add(conn, 1, "엔지니어링·R&D")
    target_fields.write_fields(conn, target_fields.choices())

    assert discard.off_target_ids(conn) == []


def test_버린_공고는_까닭과_함께_남고_다시_수집되지_않는다(conn: sqlite3.Connection) -> None:
    raw_id = add(conn, 1, "엔지니어링·R&D")

    raw, normalized, _ = discard.discard(conn, [raw_id], reason=discard.OFF_TARGET)

    assert (raw, normalized) == (1, 1)
    row = conn.execute("SELECT source_url, title, reason FROM discarded_jobs").fetchone()
    assert tuple(row) == ("https://x/1", "공고 1", discard.OFF_TARGET)
    assert runner._is_known(conn, 1, "source_url", "https://x/1")
    assert conn.execute("SELECT count(*) FROM normalized_jobs").fetchone()[0] == 0
