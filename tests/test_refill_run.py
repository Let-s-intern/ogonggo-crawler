"""한꺼번에 AI 다시 채우기를 끝까지 돌고, 진행과 마감일 변화를 남기는 것 (`app/classify/refill.py`).

모델은 부르지 않는다. 분류는 정해 둔 공고의 마감일을 바꾸고 끝을 알리는 대역이다.

| 확인 | 깨지면 |
|---|---|
| 고른 것이 묶음보다 많아도 끝까지 돈다 | 다시 누르면 앞 묶음에 돈이 또 든다 |
| 멈추기는 지금 묶음을 끝내고 멈춘다 | 수백 건이 다 돌 때까지 비용을 막을 길이 없다 |
| 끝나면 마감일 변화와 이미 보낸 공고를 센다 | 오공고 값을 바꿀 공고가 몇 건인지 모른다 |
| 도는 중에 다시 걸면 막는다 | 같은 공고가 두 번 돈다 |
| 목록 위 조각이 진행·결과·바꾸기 단추를 그린다 | 운영자가 어디까지 돌았는지 모른다 |
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import crawlers as crawlers_api
from app.api.rules import get_connect_factory
from app.classify import refill
from app.classify.batch import JOB_DONE, JOB_RUNNING, ClassifyRun, get_classify_run
from app.main import app

LIST_URL = "https://refill.example.test/"


@pytest.fixture
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "jobs.db"


@pytest.fixture
def conn(db_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(db_path)
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES ('다시 채우기', ?, 'promoted')",
        (LIST_URL,),
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '다시 채우기')")
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


def add_job(
    conn: sqlite3.Connection, raw_job_id: int, *, end: str | None = None, sent: bool = False
) -> None:
    source_url = f"{LIST_URL}{raw_job_id}/"
    conn.execute(
        "INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)"
        " VALUES (?, 1, ?, '{}', ?)",
        (raw_job_id, source_url, f"hash-{raw_job_id}"),
    )
    conn.execute(
        "INSERT INTO normalized_jobs (raw_job_id, company_name, title, source_url,"
        " recruitment_end_at) VALUES (?, '예시회사', ?, ?, ?)",
        (raw_job_id, f"공고 {raw_job_id}", source_url, end),
    )
    if sent:
        conn.execute(
            "INSERT INTO spring_deliveries (source_url, spring_job_id, status)"
            " VALUES (?, ?, 'sent')",
            (source_url, raw_job_id),
        )
    conn.commit()


def fake_classify(
    deadlines: dict[int, str] | None = None,
    seen: list[list[int]] | None = None,
    during: Callable[[list[int]], None] | None = None,
) -> Any:
    """분류 대역. 정해 둔 공고의 마감일을 바꾸고 공고마다 시작과 끝을 알린다."""

    async def classify(
        conn: sqlite3.Connection, ids: list[int], progress: Any, on_job: Any = None
    ) -> Any:
        if seen is not None:
            seen.append(list(ids))
        if during is not None:
            during(ids)
        for raw_job_id in ids:
            on_job(raw_job_id, JOB_RUNNING)
            if deadlines and raw_job_id in deadlines:
                conn.execute(
                    "UPDATE normalized_jobs SET recruitment_end_at = ? WHERE raw_job_id = ?",
                    (deadlines[raw_job_id], raw_job_id),
                )
                conn.commit()
            on_job(raw_job_id, JOB_DONE)
        return progress

    return classify


def test_묶음보다_많이_골라도_끝까지_돈다(
    conn: sqlite3.Connection, db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = list(range(1, refill.CHUNK * 2 + 6))
    for raw_job_id in ids:
        add_job(conn, raw_job_id)
    seen: list[list[int]] = []
    monkeypatch.setattr(refill, "classify_ids", fake_classify(seen=seen))
    board = refill.RefillBoard()

    started = board.start(lambda: db.connect(db_path), ClassifyRun(), ids)
    assert board.wait(5)

    assert started == len(ids)
    assert [len(chunk) for chunk in seen] == [refill.CHUNK, refill.CHUNK, 5]
    run = board.run()
    assert run is not None
    assert (run.total, run.done, run.failed, run.running, run.stopped) == (
        len(ids),
        len(ids),
        0,
        False,
        False,
    )


def test_멈추기는_지금_묶음을_끝내고_멈춘다(
    conn: sqlite3.Connection, db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = list(range(1, refill.CHUNK * 3 + 1))
    for raw_job_id in ids:
        add_job(conn, raw_job_id)
    board = refill.RefillBoard()
    seen: list[list[int]] = []
    monkeypatch.setattr(
        refill, "classify_ids", fake_classify(seen=seen, during=lambda _ids: board.stop())
    )

    board.start(lambda: db.connect(db_path), ClassifyRun(), ids)
    assert board.wait(5)

    assert len(seen) == 1
    run = board.run()
    assert run is not None
    assert run.stopped and not run.running
    assert run.done == refill.CHUNK
    assert run.left == refill.CHUNK * 2
    # 손대지 않은 공고에 `AI 대기` 가 남지 않는다
    assert board.state(ids[-1]) is None


def test_끝나면_마감일_변화와_이미_보낸_공고를_센다(
    conn: sqlite3.Connection, db_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_job(conn, 1, sent=True)  # 비었다가 찾음, 보냄
    add_job(conn, 2)  # 비었다가 찾음, 안 보냄
    add_job(conn, 3, end="2026-10-01 23:59:00", sent=True)  # 다른 날로 바뀜, 보냄
    add_job(conn, 4, end="2026-10-31 18:00:00")  # 시각만 다름 — 같은 마감
    add_job(conn, 5, sent=True)  # 그대로 비었음
    monkeypatch.setattr(
        refill,
        "classify_ids",
        fake_classify(
            {
                1: "2026-11-02 23:59:00",
                2: "2026-11-03 00:00:00",
                3: "2026-10-20 23:59:00",
                4: "2026-10-31 23:59:00",
            }
        ),
    )
    board = refill.RefillBoard()

    board.start(lambda: db.connect(db_path), ClassifyRun(), [1, 2, 3, 4, 5])
    assert board.wait(5)

    run = board.run()
    assert run is not None
    assert [(c.raw_job_id, c.before, c.after, c.sent) for c in run.found] == [
        (1, "", "2026-11-02", True),
        (2, "", "2026-11-03", False),
    ]
    assert [(c.raw_job_id, c.before, c.after) for c in run.changed] == [
        (3, "2026-10-01", "2026-10-20")
    ]
    assert [c.raw_job_id for c in run.sent_changes()] == [1, 3]


@pytest.fixture
def client(db_path: pathlib.Path, conn: sqlite3.Connection) -> Iterator[TestClient]:
    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(db_path)
        try:
            yield connection
        finally:
            connection.close()

    app.dependency_overrides[crawlers_api.get_connection] = request_connection
    app.dependency_overrides[get_classify_run] = ClassifyRun
    app.dependency_overrides[get_connect_factory] = lambda: lambda: db.connect(db_path)
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_목록_위에_결과와_보낸_공고_바꾸기_단추가_뜬다(
    client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_job(conn, 1, sent=True)
    add_job(conn, 2)
    board = refill.RefillBoard()
    monkeypatch.setattr(refill, "_board", board)
    monkeypatch.setattr(refill, "classify_ids", fake_classify({1: "2026-11-02", 2: "2026-11-03"}))

    assert client.get("/ui/review/refill-status").text.strip() == ""
    started = client.post("/ui/review/reclassify", data={"all_filtered": "1", "view": "all"})
    assert board.wait(5)

    assert "다시 누르지 않아도 된다" in started.text
    assert "끝난 뒤 다시 누른다" not in started.text
    html = client.get("/ui/review/refill-status").text
    assert "AI 다시 채우기 끝" in html
    assert '2건 중 <span class="font-semibold">2건</span> 처리' in html
    assert '마감일 새로 찾음 <span class="font-semibold">2건</span>' in html
    assert "이 1건 오공고 값 바꾸기" in html
    assert '<input type="hidden" name="raw_job_id" value="1">' in html
    assert 'name="raw_job_id" value="2"' not in html

    client.post("/ui/review/refill-status/dismiss")
    assert client.get("/ui/review/refill-status").text.strip() == ""


def test_도는_중에_다시_걸면_막고_진행을_알려준다(
    client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_job(conn, 1)
    board = refill.RefillBoard()
    monkeypatch.setattr(refill, "_board", board)
    board._run = refill.RefillRun(total=40, done=12, running=True)

    html = client.post("/ui/review/reclassify", data={"raw_job_id": "1"}).text

    assert "이미 돌고 있다(40건 중 12건 처리)" in html
    status = client.get("/ui/review/refill-status").text
    assert "AI 다시 채우는 중" in status
    assert 'hx-trigger="every 3s"' in status
    assert "멈추기" in status


def test_막대가_끝을_보면_표를_다시_부른다(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    board = refill.RefillBoard()
    monkeypatch.setattr(refill, "_board", board)
    board._run = refill.RefillRun(total=3, done=3, running=False)

    assert client.get("/ui/review/refill-status?polling=1").headers["HX-Trigger"] == "jobs-deleted"
    # 처음 그릴 때나 목록이 다시 그려질 때는 부르지 않는다 — 부르면 서로를 끝없이 부른다
    assert "HX-Trigger" not in client.get("/ui/review/refill-status").headers
