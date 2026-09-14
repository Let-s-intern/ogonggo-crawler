"""본문을 얻지 못한 공고는 `raw_jobs` 에 넣지 않는다 (2.1).

목록에서 읽은 값만 넣고 성공으로 넘기던 경로가 본문 없는 행 86건을 만들었다
(`.claude/tasks/done/fill-body/prd-fill-body.md`). 그 경로를 막았는지 본다.

실사이트에 나가지 않는다. 목록은 저장된 python.org 픽스처이고, 상세는 본문만 비운 스텁이
돌려준다. DB 는 임시 파일에 마이그레이션을 올려 쓴다.
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from app import db
from app.crawler.collect import Collectors
from app.crawler.parser import DetailParseResult, ListItem, ListParseResult
from app.crawler.runner import SCHEDULE, RunTarget, run_once
from app.selector.schema import DETAIL_FIELDS, validate_selectors

LIST_URL = "https://example.test/jobs"

SELECTORS = validate_selectors(
    {
        "list": {"item": "ul.jobs > li", "title": "h3", "link": "a", "date": "span.d"},
        "detail": {
            "title": "p.title",
            "body": "div.body",
            "qualifications": "",
            "recruitment_end_at": "",
            "department": "",
        },
    }
)

ITEMS = [
    ListItem(index=0, title="백엔드 개발자", link=f"{LIST_URL}/1", date=""),
    ListItem(index=1, title="프론트엔드 개발자", link=f"{LIST_URL}/2", date=""),
]


class StubList:
    """목록은 항상 같은 두 건을 돌려준다."""

    def __init__(self, items: list[ListItem]) -> None:
        self._items = items

    async def collect(self) -> ListParseResult:
        return ListParseResult(matched=len(self._items), items=list(self._items), failures=[])


class StubDetail:
    """상세 필드를 손으로 준다. `body` 를 비워 본문 없는 응답을 흉내낸다."""

    def __init__(self, body: str) -> None:
        self._body = body
        self.calls: list[str] = []

    async def collect(self, item: ListItem) -> DetailParseResult:
        self.calls.append(item.link)
        fields = dict.fromkeys(DETAIL_FIELDS, "")
        fields["title"] = item.title
        fields["body"] = self._body
        return DetailParseResult(fields=fields, missing=[])


def collectors(detail: StubDetail, items: list[ListItem] = ITEMS) -> Collectors:
    return Collectors(list_mode="static", detail_mode="static", list=StubList(items), detail=detail)


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES (?, ?, 'draft')",
        ("예시", LIST_URL),
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '예시 채용')")
    try:
        yield connection
    finally:
        connection.close()


def target() -> RunTarget:
    return RunTarget(list_url=LIST_URL, selectors=SELECTORS, trigger=SCHEDULE, workflow_id=1)


async def test_본문이_빈_상세는_적재하지_않고_detail_empty_로_남는다(
    conn: sqlite3.Connection,
) -> None:
    result = await run_once(conn, target(), collectors=collectors(StubDetail("")), limit=1)

    assert conn.execute("SELECT * FROM raw_jobs").fetchall() == []
    assert (result.success_count, result.new_count, result.fail_count) == (0, 0, 1)
    assert [failure.error_class for failure in result.failures] == ["detail_empty"]


async def test_어느_공고의_본문이_비었는지가_제목과_함께_남는다(
    conn: sqlite3.Connection,
) -> None:
    """주소만으로는 어느 공고인지 모른다. 목록에서 읽은 제목이 같이 있어야 고칠 수 있다."""
    result = await run_once(conn, target(), collectors=collectors(StubDetail("   ")), limit=1)

    rows = conn.execute(
        "SELECT reason, title, source_url, message FROM crawl_run_failures WHERE run_id = ?",
        (result.run_id,),
    ).fetchall()

    assert len(rows) == 1
    assert rows[0]["reason"] == "detail_empty"
    assert rows[0]["title"] == "백엔드 개발자"
    assert rows[0]["source_url"] == f"{LIST_URL}/1"
    assert "본문" in rows[0]["message"]


async def test_본문이_있는_건은_그대로_적재된다(conn: sqlite3.Connection) -> None:
    """막은 것은 본문 없는 건뿐이다. 정상 건까지 떨어지면 수집이 멈춘다."""
    result = await run_once(conn, target(), collectors=collectors(StubDetail("본문")), limit=2)

    assert len(conn.execute("SELECT * FROM raw_jobs").fetchall()) == 2
    assert (result.success_count, result.new_count, result.fail_count) == (2, 2, 0)
    assert result.status == "success"


async def test_상세로_갈_길이_없으면_detail_unreachable_이다(
    conn: sqlite3.Connection,
) -> None:
    """목록 주소만 가진 행이 쌓이던 경로다. 도달 실패와 본문 없음은 고칠 자리가 다르다."""
    detail = StubDetail("본문")
    items = [ListItem(index=0, title="상시 채용", link=LIST_URL, date="", detail_absent=True)]

    result = await run_once(conn, target(), collectors=collectors(detail, items), limit=1)

    assert conn.execute("SELECT * FROM raw_jobs").fetchall() == []
    assert [failure.error_class for failure in result.failures] == ["detail_unreachable"]
    # 상세로 갈 길이 없으니 상세 수집기를 부르지도 않는다
    assert detail.calls == []
    row = conn.execute(
        "SELECT reason, title FROM crawl_run_failures WHERE run_id = ?", (result.run_id,)
    ).fetchone()
    assert (row["reason"], row["title"]) == ("detail_unreachable", "상시 채용")


# 목록이 항목을 하나도 내놓지 않은 실행 (2.2) --------------------


async def test_빈_목록은_list_empty_로_실패한다(conn: sqlite3.Connection) -> None:
    """항목 0건은 신규 0건인 정상 실행이 아니다. 사유가 없으면 원인 모를 실행과 구분되지 않는다."""
    result = await run_once(
        conn, target(), collectors=collectors(StubDetail("본문"), items=[]), limit=3
    )

    assert result.status == "failed"
    assert result.error_class == "list_empty"
    assert (result.success_count, result.new_count, result.fail_count) == (0, 0, 0)

    row = conn.execute(
        "SELECT status, error_class, error_message FROM crawl_runs WHERE id = ?", (result.run_id,)
    ).fetchone()
    assert (row["status"], row["error_class"]) == ("failed", "list_empty")
    assert row["error_message"]


async def test_항목별_실패가_있으면_그_사유가_이긴다(conn: sqlite3.Connection) -> None:
    """어느 공고를 왜 놓쳤는지 이미 알고 있다. 그것을 list_empty 로 덮으면 조치가 갈리지 않는다."""
    items = [ListItem(index=0, title="상시 채용", link=LIST_URL, date="", detail_absent=True)]

    result = await run_once(
        conn, target(), collectors=collectors(StubDetail("본문"), items=items), limit=1
    )

    assert result.status == "failed"
    assert result.error_class == "detail_unreachable"
