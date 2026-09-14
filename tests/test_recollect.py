"""원문 다시 수집 (2026-09-14).

실사이트에 나가지 않는다. 목록·상세 수집기를 가짜로 끼우고, 다시 분류는 대상만 적어 두는 함수로
바꾼다.

| 확인 | 깨지면 |
|---|---|
| 바뀐 공고는 같은 행에 갈아 끼우고 지금 값은 이력에 남긴다 | 지금 값이 사라지거나 보정이 끊긴다 |
| 값이 같으면 갈아 끼우지도 남기지도 않는다 | 이력이 쓸데없이 쌓인다 |
| 저장된 마감일이 지난 공고는 열지 않는다 | 내려간 공고마다 요청이 나가고 실패가 쌓인다 |
| 못 가져오거나 본문이 비면 지금 값을 둔다 | 실패한 공고의 원문이 빈 값으로 덮인다 |
| 목록에 있으면 목록 항목으로, 없으면 저장된 주소로 연다 | 상세 API 사이트가 id 를 모른다 |
| 목록이 더는 주지 않는 칸은 지금 값을 둔다 | 다시 수집이 목록 칸을 비운다 |
| 다시 분류는 워크플로우 공고 전부다 | 마감 공고·안 바뀐 공고가 옛 분류로 남는다 |
| 다른 분류가 돌면 기다렸다가 한다 | 같은 공고에 두 번 돈을 쓴다 |
| 실행 기록은 recollect 이고 연속 실패에 세지 않는다 | 다시 수집 실패가 주기 수집을 멈춘다 |
| 검수 화면 삭제가 이력까지 지운다 | 이력이 붙은 공고를 지우면 외래키로 죽는다 |
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
from app.api.review_filter import _delete_rows
from app.classify.batch import ClassifyProgress, get_classify_run
from app.crawler import recollect
from app.crawler.collect import Collectors
from app.crawler.failures import SUCCESS
from app.crawler.parser import DetailParseResult, ListItem, ListParseResult
from app.crawler.runner import RunResult, _record, consecutive_failures
from app.selector.schema import SPLIT_DETAIL_FIELDS
from tests.test_ui_workflow_run import add_workflow

URL_A = "https://jobs.example.test/1"
URL_B = "https://jobs.example.test/2"
URL_C = "https://jobs.example.test/3"


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    try:
        yield connection
    finally:
        connection.close()


def stored_record(url: str, **overrides: str) -> dict[str, str]:
    return {
        "source_url": url,
        "title": "제목",
        "body": "옛 본문",
        "qualifications": "",
        "recruitment_end_at": "",
        "department": "",
        "company_name": "",
        **{name: "" for name in SPLIT_DETAIL_FIELDS},
        "list_title": "제목",
        "list_date": "",
        **overrides,
    }


def add_job(conn: sqlite3.Connection, workflow_id: int, record: dict[str, Any]) -> int:
    cursor = conn.execute(
        """
        INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash, crawled_at)
        VALUES (?, ?, ?, ?, '2026-09-01 00:00:00')
        """,
        (workflow_id, record["source_url"], json.dumps(record, ensure_ascii=False), "옛-해시"),
    )
    return int(cursor.lastrowid or 0)


def raw(conn: sqlite3.Connection, raw_job_id: int) -> sqlite3.Row:
    row: sqlite3.Row = conn.execute("SELECT * FROM raw_jobs WHERE id = ?", (raw_job_id,)).fetchone()
    return row


def detail(body: str = "새 본문", source_text: str = "새 원문", **fields: str) -> DetailParseResult:
    values = {
        "title": "제목",
        "body": body,
        "qualifications": "",
        "recruitment_end_at": "",
        "department": "",
        "company_name": "",
        **{name: "" for name in SPLIT_DETAIL_FIELDS},
        **fields,
    }
    return DetailParseResult(fields=values, missing=[], source_text=source_text)


class FakeList:
    def __init__(self, items: list[ListItem] | None = None, error: Exception | None = None) -> None:
        self._items = items or []
        self._error = error

    async def collect(self) -> ListParseResult:
        if self._error is not None:
            raise self._error
        return ListParseResult(matched=len(self._items), items=self._items, failures=[])


class FakeDetail:
    def __init__(self, pages: dict[str, DetailParseResult | Exception]) -> None:
        self._pages = pages
        self.opened: list[ListItem] = []

    async def collect(self, item: ListItem) -> DetailParseResult:
        self.opened.append(item)
        page = self._pages[item.link]
        if isinstance(page, Exception):
            raise page
        return page


def collectors(
    pages: dict[str, DetailParseResult | Exception],
    items: list[ListItem] | None = None,
    list_error: Exception | None = None,
) -> Collectors:
    return Collectors(
        list_mode="static",
        detail_mode="static",
        list=FakeList(items, list_error),
        detail=FakeDetail(pages),
    )


def opened(active: Collectors) -> list[ListItem]:
    assert isinstance(active.detail, FakeDetail)
    return active.detail.opened


class Reclassified:
    """다시 분류할 공고만 적어 둔다. 모델을 부르지 않는다."""

    def __init__(self) -> None:
        self.ids: list[list[int]] = []

    async def __call__(
        self, conn: sqlite3.Connection, ids: list[int], progress: ClassifyProgress
    ) -> None:
        self.ids.append(list(ids))
        progress.total = len(ids)
        progress.processed = len(ids)
        progress.total_tokens = 1234


async def recollect_now(
    conn: sqlite3.Connection,
    workflow_id: int,
    active: Collectors,
    reclassify: Reclassified | None = None,
) -> RunResult:
    return await recollect.recollect_workflow(
        conn,
        workflow_id,
        collectors=active,
        reclassify=reclassify or Reclassified(),
        wait_seconds=0.01,
    )


def failures(conn: sqlite3.Connection, run_id: int) -> list[tuple[str | None, str, str]]:
    return [
        (row["reason"], row["source_url"], row["message"])
        for row in conn.execute(
            "SELECT reason, source_url, message FROM crawl_run_failures"
            " WHERE run_id = ? ORDER BY id",
            (run_id,),
        )
    ]


async def test_바뀐_공고는_같은_행에_갈아_끼우고_지금_값은_이력에_남긴다(
    conn: sqlite3.Connection,
) -> None:
    workflow_id = add_workflow(conn)
    job = add_job(conn, workflow_id, stored_record(URL_A))
    before = raw(conn, job)

    result = await recollect_now(conn, workflow_id, collectors({URL_A: detail()}))

    after = raw(conn, job)
    record = json.loads(after["raw_data_json"])
    assert (record["body"], record["source_text"]) == ("새 본문", "새 원문")
    assert after["crawled_at"] == before["crawled_at"]
    assert after["content_hash"] != before["content_hash"]
    (history,) = conn.execute("SELECT * FROM raw_job_history").fetchall()
    assert history["raw_job_id"] == job
    assert history["run_id"] == result.run_id
    assert history["raw_data_json"] == before["raw_data_json"]
    assert history["content_hash"] == before["content_hash"]
    assert history["crawled_at"] == before["crawled_at"]
    assert (result.status, result.success_count, result.skipped_count, result.fail_count) == (
        SUCCESS,
        1,
        0,
        0,
    )


async def test_값이_같으면_갈아_끼우지도_이력에_남기지도_않는다(conn: sqlite3.Connection) -> None:
    workflow_id = add_workflow(conn)
    page = detail()
    same = _record(
        ListItem(index=0, title="제목", link=URL_A, date=""), page.fields, page.source_text
    )
    job = add_job(conn, workflow_id, same)
    before = raw(conn, job)

    result = await recollect_now(conn, workflow_id, collectors({URL_A: page}))

    assert raw(conn, job)["raw_data_json"] == before["raw_data_json"]
    assert conn.execute("SELECT count(*) FROM raw_job_history").fetchone()[0] == 0
    assert result.success_count == 1


async def test_저장된_마감일이_지난_공고는_열지_않는다(conn: sqlite3.Connection) -> None:
    workflow_id = add_workflow(conn)
    add_job(conn, workflow_id, stored_record(URL_A, recruitment_end_at="2020-01-01"))
    add_job(conn, workflow_id, stored_record(URL_B))
    active = collectors({URL_B: detail()})

    result = await recollect_now(conn, workflow_id, active)

    assert [item.link for item in opened(active)] == [URL_B]
    assert (result.success_count, result.skipped_count) == (1, 1)


async def test_상세를_못_가져오면_지금_값을_두고_실패로_남긴다(conn: sqlite3.Connection) -> None:
    workflow_id = add_workflow(conn)
    job = add_job(conn, workflow_id, stored_record(URL_A))
    before = raw(conn, job)

    result = await recollect_now(
        conn, workflow_id, collectors({URL_A: RuntimeError("연결이 끊겼다")})
    )

    assert raw(conn, job)["raw_data_json"] == before["raw_data_json"]
    assert result.fail_count == 1
    assert any(
        url == URL_A and "연결이 끊겼다" in message
        for _, url, message in failures(conn, result.run_id)
    )


async def test_상세_본문이_비면_지금_값을_둔다(conn: sqlite3.Connection) -> None:
    workflow_id = add_workflow(conn)
    job = add_job(conn, workflow_id, stored_record(URL_A))
    before = raw(conn, job)

    result = await recollect_now(conn, workflow_id, collectors({URL_A: detail(body="")}))

    assert raw(conn, job)["raw_data_json"] == before["raw_data_json"]
    assert ("detail_empty", URL_A) in [
        (reason, url) for reason, url, _ in failures(conn, result.run_id)
    ]


async def test_목록에_있으면_목록_항목으로_없으면_저장된_주소로_연다(
    conn: sqlite3.Connection,
) -> None:
    workflow_id = add_workflow(conn)
    add_job(conn, workflow_id, stored_record(URL_A))
    add_job(conn, workflow_id, stored_record(URL_B, list_title="저장된 목록 제목"))
    listed = ListItem(index=3, title="목록 제목", link=URL_A, date="", detail_key="42")
    active = collectors({URL_A: detail(), URL_B: detail()}, items=[listed])

    await recollect_now(conn, workflow_id, active)

    by_link = {item.link: item for item in opened(active)}
    assert by_link[URL_A].detail_key == "42"
    assert by_link[URL_B].detail_key == ""
    assert by_link[URL_B].title == "저장된 목록 제목"


async def test_목록을_못_읽어도_저장된_주소로_열고_목록이_주던_칸은_지금_값을_둔다(
    conn: sqlite3.Connection,
) -> None:
    workflow_id = add_workflow(conn)
    kept = SPLIT_DETAIL_FIELDS[0]
    job = add_job(conn, workflow_id, stored_record(URL_A, **{kept: "목록이 주던 값"}))

    result = await recollect_now(
        conn,
        workflow_id,
        collectors({URL_A: detail()}, list_error=RuntimeError("목록이 막혔다")),
    )

    assert json.loads(raw(conn, job)["raw_data_json"])[kept] == "목록이 주던 값"
    assert any(
        reason is None and message.startswith("목록을 읽지 못해")
        for reason, _, message in failures(conn, result.run_id)
    )


async def test_다시_분류는_워크플로우_공고_전부다(conn: sqlite3.Connection) -> None:
    workflow_id = add_workflow(conn)
    closed = add_job(conn, workflow_id, stored_record(URL_A, recruitment_end_at="2020-01-01"))
    failing = add_job(conn, workflow_id, stored_record(URL_B))
    changed = add_job(conn, workflow_id, stored_record(URL_C))
    other = add_workflow(conn, name="다른 사이트")
    add_job(conn, other, stored_record(URL_A))
    reclassified = Reclassified()

    result = await recollect_now(
        conn,
        workflow_id,
        collectors({URL_B: RuntimeError("실패"), URL_C: detail()}),
        reclassified,
    )

    assert reclassified.ids == [[changed, failing, closed]]
    notes = [message for reason, _, message in failures(conn, result.run_id) if reason is None]
    assert any(
        message.startswith("다시 분류: 3건 중 처리 3건, 실패 0건, 토큰 1,234개")
        for message in notes
    )


async def test_다른_분류가_돌면_끝날_때까지_기다렸다가_다시_분류한다(
    conn: sqlite3.Connection,
) -> None:
    workflow_id = add_workflow(conn)
    add_job(conn, workflow_id, stored_record(URL_A))
    run = get_classify_run()
    run.claim()
    reclassified = Reclassified()

    async def release_later() -> None:
        await asyncio.sleep(0.05)
        assert reclassified.ids == []
        run.release()

    try:
        releasing = asyncio.create_task(release_later())
        await recollect_now(conn, workflow_id, collectors({URL_A: detail()}), reclassified)
        await releasing
    finally:
        if run.progress().running:
            run.release()

    assert len(reclassified.ids) == 1
    assert run.progress().running is False


async def test_실행_기록은_recollect_이고_연속_실패에_세지_않는다(conn: sqlite3.Connection) -> None:
    workflow_id = add_workflow(conn)
    add_job(conn, workflow_id, stored_record(URL_A))

    result = await recollect_now(conn, workflow_id, collectors({URL_A: RuntimeError("실패")}))

    row = conn.execute(
        "SELECT trigger, status FROM crawl_runs WHERE id = ?", (result.run_id,)
    ).fetchone()
    assert (row["trigger"], row["status"]) == ("recollect", "failed")
    assert consecutive_failures(conn, workflow_id, 5) == 0


async def test_진행_상황은_끝나면_사라진다(conn: sqlite3.Connection) -> None:
    workflow_id = add_workflow(conn)
    add_job(conn, workflow_id, stored_record(URL_A))
    seen: list[str] = []

    class Watching(Reclassified):
        async def __call__(
            self, conn: sqlite3.Connection, ids: list[int], progress: ClassifyProgress
        ) -> None:
            seen.append(recollect.PROGRESS[workflow_id].stage)
            await super().__call__(conn, ids, progress)

    await recollect_now(conn, workflow_id, collectors({URL_A: detail()}), Watching())

    assert seen == ["분류"]
    assert workflow_id not in recollect.PROGRESS


async def test_검수_화면_삭제가_이력까지_지운다(conn: sqlite3.Connection) -> None:
    workflow_id = add_workflow(conn)
    job = add_job(conn, workflow_id, stored_record(URL_A))
    await recollect_now(conn, workflow_id, collectors({URL_A: detail()}))

    _delete_rows(conn, [job])

    assert conn.execute("SELECT count(*) FROM raw_job_history").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM raw_jobs").fetchone()[0] == 0


def test_시작_전_확인은_마감과_다시_분류_건수를_센다(conn: sqlite3.Connection) -> None:
    workflow_id = add_workflow(conn)
    add_job(conn, workflow_id, stored_record(URL_A, recruitment_end_at="2020-01-01"))
    add_job(conn, workflow_id, stored_record(URL_B))
    add_job(conn, workflow_id, stored_record(URL_C))
    add_job(conn, add_workflow(conn, name="다른 사이트"), stored_record(URL_A))

    assert recollect.preview(conn, workflow_id) == recollect.RecollectPreview(
        total=3, targets=2, closed=1, classify=3
    )


def test_실행_출처에_recollect_를_받고_모르는_값은_거절한다(conn: sqlite3.Connection) -> None:
    workflow_id = add_workflow(conn)

    conn.execute(
        "INSERT INTO crawl_runs (workflow_id, trigger) VALUES (?, 'recollect')", (workflow_id,)
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO crawl_runs (workflow_id, trigger) VALUES (?, 'nope')", (workflow_id,)
        )


def test_되돌리면_이력_표가_사라지고_recollect_실행은_수동으로_남는다(
    conn: sqlite3.Connection,
) -> None:
    workflow_id = add_workflow(conn)
    conn.execute(
        "INSERT INTO crawl_runs (workflow_id, trigger) VALUES (?, 'recollect')", (workflow_id,)
    )

    # 0031 이 뒤에 붙었으니 두 걸음을 되돌려야 0030 이 풀린다
    db.migrate_down(conn, steps=2)

    assert conn.execute("SELECT trigger FROM crawl_runs").fetchone()["trigger"] == "manual"
    tables = {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "raw_job_history" not in tables
