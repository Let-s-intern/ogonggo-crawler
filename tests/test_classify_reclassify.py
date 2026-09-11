"""이미 나눈 공고를 다시 분류할 때 나눈 목록은 그대로 두고 칸만 다시 채운다 (2026-09-11 결정).

번호에 사람 보정과 전달된 공고 주소가 붙어 있어, 개수나 순서가 바뀌면 그 값이 다른 직무로
옮겨 붙는다. 한 번도 나누지 않은 공고(1번 하나, 직무 이름·보낸 줄 없음)는 나눌 수 있다.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from app import db
from app.classify.batch import ClassifyProgress, classify_ids
from app.classify.schema import Classification
from app.classify.store import StoredPart, read_parts
from app.config import Settings
from tests import test_classify_long as long_posting
from tests import test_classify_split as split_posting
from tests.test_selector_generator import FakeClient

SHORT = 1
LONG = 2


def settings_with_key() -> Settings:
    return Settings(gemini_api_key="테스트키", gemini_model="gemini-3.5-flash")


def _seed_raw(connection: sqlite3.Connection, raw_id: int, title: str, body: str) -> None:
    raw = {"source_url": f"https://x/{raw_id}", "title": title, "body": body}
    connection.execute(
        "INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)"
        " VALUES (?, 1, ?, ?, ?)",
        (raw_id, raw["source_url"], json.dumps(raw, ensure_ascii=False), f"hash{raw_id}"),
    )


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (id, name, list_url, status)"
        " VALUES (1, '테스트', 'https://x', 'promoted')"
    )
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, '테스트')")
    _seed_raw(connection, SHORT, split_posting.TITLE, split_posting.BODY)
    _seed_raw(connection, LONG, long_posting.TITLE, long_posting.BODY)
    try:
        yield connection
    finally:
        connection.close()


def store_parts(
    conn: sqlite3.Connection, raw_id: int, parts: list[tuple[int, str | None, str | None]]
) -> None:
    """먼저 한 번 분류된 상태. 칸에는 옛 값이 들어 있다."""
    conn.executemany(
        "INSERT INTO job_classifications (raw_job_id, part, part_role, part_lines, model, duties)"
        " VALUES (?, ?, ?, ?, 'old-model', '옛 업무')",
        [(raw_id, *part) for part in parts],
    )


async def reclassify(
    conn: sqlite3.Connection, raw_id: int, *texts: str
) -> tuple[ClassifyProgress, FakeClient]:
    client = FakeClient(*texts)
    progress = ClassifyProgress()
    await classify_ids(conn, [raw_id], progress, client=client, settings=settings_with_key())
    return progress, client


def rows(conn: sqlite3.Connection, raw_id: int, columns: str) -> list[tuple[object, ...]]:
    return [
        tuple(row)
        for row in conn.execute(
            f"SELECT {columns} FROM job_classifications WHERE raw_job_id = ? ORDER BY part",
            (raw_id,),
        )
    ]


async def test_한_번에_나눈_공고는_직무_목록을_알려주고_칸만_다시_채운다(
    conn: sqlite3.Connection,
) -> None:
    store_parts(conn, SHORT, [(1, "로봇 SW 개발", None), (2, "비전 AI 연구", None)])

    progress, client = await reclassify(conn, SHORT, split_posting.SPLIT)

    assert (progress.processed, progress.failed) == (1, 0)
    prompt = client.calls[0]["contents"]
    assert "정확히 2개" in prompt
    assert "1. 로봇 SW 개발" in prompt
    assert rows(conn, SHORT, "part, duties") == [
        (1, "로봇 제어 소프트웨어를 개발합니다"),
        (2, "영상 인식 모델을 연구합니다"),
    ]


async def test_목록을_고정한_공고는_직무_이름도_저장된_것을_쓴다(conn: sqlite3.Connection) -> None:
    store_parts(conn, SHORT, [(1, "첫 직무", None), (2, "둘째 직무", None)])

    await reclassify(conn, SHORT, split_posting.SPLIT)

    assert rows(conn, SHORT, "part, part_role") == [(1, "첫 직무"), (2, "둘째 직무")]


async def test_개수가_다르면_실패로_두고_기존_값을_그대로_둔다(conn: sqlite3.Connection) -> None:
    before = [(1, "가", None), (2, "나", None), (3, "다", None)]
    store_parts(conn, SHORT, before)

    progress, _ = await reclassify(conn, SHORT, split_posting.SPLIT)

    assert (progress.processed, progress.failed) == (0, 1)
    assert "3개" in progress.errors[0]
    assert rows(conn, SHORT, "part, part_role, duties") == [
        (1, "가", "옛 업무"),
        (2, "나", "옛 업무"),
        (3, "다", "옛 업무"),
    ]


async def test_긴_공고는_짜임을_다시_묻지_않고_보냈던_줄을_그대로_보낸다(
    conn: sqlite3.Connection,
) -> None:
    store_parts(conn, LONG, [(1, "기계", "[[1, 4]]"), (2, "HR", "[[1, 2], [5, 6]]")])

    progress, client = await reclassify(conn, LONG, long_posting.MECHANICAL, long_posting.HR)

    assert progress.processed == 1
    assert len(client.calls) == 2
    assert all(call["config"]["response_schema"] is Classification for call in client.calls)
    assert "[3] [기계]" in client.calls[0]["contents"]
    assert "[5] [HR]" not in client.calls[0]["contents"]
    assert rows(conn, LONG, "part, part_role, part_lines, duties") == [
        (1, "기계", "[[1, 4]]", "설비 설계 업무를 합니다"),
        (2, "HR", "[[1, 2], [5, 6]]", "인사 제도를 운영합니다"),
    ]


async def test_한_번도_나누지_않은_공고는_다시_분류할_때_나눌_수_있다(
    conn: sqlite3.Connection,
) -> None:
    store_parts(conn, SHORT, [(1, None, None)])

    _, client = await reclassify(conn, SHORT, split_posting.SPLIT)

    assert "이미 나눈 직무" not in client.calls[0]["contents"]
    assert rows(conn, SHORT, "part, part_role") == [(1, "로봇 SW 개발"), (2, "비전 AI 연구")]


def test_저장된_줄은_번호로_풀어_읽고_읽지_못하면_줄이_없는_공고다(
    conn: sqlite3.Connection,
) -> None:
    store_parts(conn, LONG, [(1, "기계", "[[1, 4]]"), (2, None, "깨진 값")])

    assert read_parts(conn, LONG) == [
        StoredPart(part=1, role="기계", lines=(1, 2, 3, 4)),
        StoredPart(part=2, role="", lines=()),
    ]
