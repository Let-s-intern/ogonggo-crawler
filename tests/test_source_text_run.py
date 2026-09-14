"""수집 실행이 원문을 `raw_jobs.raw_data_json.source_text` 에 넣는지 본다.

파서가 원문을 만드는지는 `tests/test_source_text.py` 가 본다. 여기는 그 값이 적재까지
그대로 가는지, 그리고 원문이 없는 건이 지금까지와 같은 모양으로 적재되는지를 본다.

실사이트에 나가지 않는다. 목록과 상세는 스텁이고 DB 는 임시 파일이다.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from app import db
from app.crawler.api_source import build_detail
from app.crawler.collect import Collectors
from app.crawler.hashing import content_hash
from app.crawler.parser import DetailParseResult, ListItem, ListParseResult
from app.crawler.runner import SCHEDULE, RunTarget, run_once
from app.selector.api_schema import validate_api_config
from app.selector.schema import DETAIL_FIELDS, SPLIT_DETAIL_FIELDS, validate_selectors

LIST_URL = "https://example.test/jobs"

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SEEDS = pathlib.Path(__file__).parent.parent / "seeds" / "site-configs-20260826.json"

# 원문을 뽑기 전에 `_record()` 가 만들던 키. 원문이 없는 건은 이 모양 그대로 적재된다
RECORD_KEYS = {
    "source_url",
    "title",
    "body",
    "qualifications",
    "recruitment_end_at",
    "department",
    "company_name",
    "list_title",
    "list_date",
    *SPLIT_DETAIL_FIELDS,
}

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

ITEMS = [ListItem(index=0, title="백엔드 개발자", link=f"{LIST_URL}/1", date="")]


class StubList:
    async def collect(self) -> ListParseResult:
        return ListParseResult(matched=len(ITEMS), items=list(ITEMS), failures=[])


class StubDetail:
    """상세 필드와 원문을 손으로 준다. 원문을 주지 않는 사이트가 기본값이다."""

    def __init__(self, body: str = "본문이다", source: str = "") -> None:
        self._body = body
        self._source = source

    async def collect(self, item: ListItem) -> DetailParseResult:
        fields = dict.fromkeys(DETAIL_FIELDS, "")
        fields["title"] = item.title
        fields["body"] = self._body
        return DetailParseResult(fields=fields, missing=[], source_text=self._source)


def collectors(detail: StubDetail) -> Collectors:
    return Collectors(list_mode="static", detail_mode="static", list=StubList(), detail=detail)


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


def stored(conn: sqlite3.Connection) -> dict[str, str]:
    row = conn.execute("SELECT raw_data_json FROM raw_jobs").fetchone()
    assert row is not None
    data: dict[str, str] = json.loads(row["raw_data_json"])
    return data


async def test_원문은_적재된_공고의_source_text_로_들어간다(conn: sqlite3.Connection) -> None:
    detail = StubDetail(body="본문이다", source="제목\n회사 예시\n본문이다\n근무지 판교")

    await run_once(conn, target(), collectors=collectors(detail), limit=1)

    data = stored(conn)
    assert data["source_text"] == "제목\n회사 예시\n본문이다\n근무지 판교"
    assert data["body"] == "본문이다"


async def test_원문은_저장할_때_손대지_않는다(conn: sqlite3.Connection) -> None:
    """줄바꿈과 공백까지 뽑은 그대로다. 정제는 정규화의 몫이다."""
    raw = "\n\n제목\n\n  회사 예시  \n\n본문이다\n\n"
    await run_once(conn, target(), collectors=collectors(StubDetail(source=raw)), limit=1)

    assert stored(conn)["source_text"] == raw


async def test_원문이_바뀌어도_같은_공고가_다시_쌓이지_않는다(conn: sqlite3.Connection) -> None:
    """조회수와 배너는 매 크롤마다 달라진다. 그것이 해시에 들어가면 같은 공고가 매번 신규다."""
    await run_once(
        conn,
        target(),
        collectors=collectors(StubDetail(source="백엔드 개발자\n조회수 1,204")),
        limit=1,
    )
    again = await run_once(
        conn,
        target(),
        collectors=collectors(StubDetail(source="백엔드 개발자\n조회수 1,881\n신규 배너")),
        limit=1,
    )

    rows = conn.execute("SELECT id FROM raw_jobs").fetchall()
    assert len(rows) == 1
    assert (again.new_count, again.skipped_count, again.fail_count) == (0, 1, 0)


async def test_해시는_원문을_담기_전과_같은_값이다(conn: sqlite3.Connection) -> None:
    """`HASH_FIELDS` 는 source_url·title·recruitment_end_at·body 넷 그대로다 (8.3)."""
    await run_once(
        conn,
        target(),
        collectors=collectors(StubDetail(source="제목\n회사 예시\n본문이다")),
        limit=1,
    )

    row = conn.execute("SELECT content_hash, raw_data_json FROM raw_jobs").fetchone()
    data = json.loads(row["raw_data_json"])
    assert data["source_text"]

    without = {name: data[name] for name in ("source_url", "title", "recruitment_end_at", "body")}
    assert row["content_hash"] == content_hash(without)


class HanwhaDetail:
    """상세가 API 인 사이트. 저장된 한화 응답을 그대로 읽는다."""

    async def collect(self, item: ListItem) -> DetailParseResult:
        payload = json.loads((FIXTURES / "hanwha-detail-20260825.json").read_text(encoding="utf-8"))
        entry = json.loads(SEEDS.read_text(encoding="utf-8"))["crawlers"]
        config = next(one for one in entry if one["name"] == "한화")["api_config"]
        return build_detail(payload, validate_api_config(config).detail_config())


async def test_원문이_없어도_공고는_그대로_적재된다(conn: sqlite3.Connection) -> None:
    """원문 자리가 아무것도 못 잡은 건이다. 원문이 없다고 공고를 버리면 이미 되는 것을 잃는다."""
    result = await run_once(conn, target(), collectors=collectors(StubDetail()), limit=1)

    assert (result.new_count, result.fail_count) == (1, 0)
    assert result.status == "success"
    assert stored(conn)["body"] == "본문이다"


async def test_원문이_없는_건은_원문을_뽑기_전과_같은_모양이다(conn: sqlite3.Connection) -> None:
    """키가 늘지 않는다. 소비 측과 정규화가 보던 모양 그대로다."""
    await run_once(conn, target(), collectors=collectors(StubDetail()), limit=1)

    assert set(stored(conn)) == RECORD_KEYS


async def test_상세가_API_인_사이트는_응답을_편_원문이_적재된다(conn: sqlite3.Connection) -> None:
    """본문 경로 밖의 근무지·모집인원까지 분류가 읽게 된다."""
    collect = Collectors(
        list_mode="static", detail_mode="api", list=StubList(), detail=HanwhaDetail()
    )

    result = await run_once(conn, target(), collectors=collect, limit=1)

    assert (result.new_count, result.fail_count) == (1, 0)
    data = stored(conn)
    assert "LIFEPLUS TV" in data["body"]
    assert "ruWorkpl: 본사(서울 63빌딩)" in data["source_text"]
    assert conn.execute("SELECT count(*) FROM normalized_jobs").fetchone()[0] == 1


async def test_공백뿐인_원문은_없는_것으로_본다(conn: sqlite3.Connection) -> None:
    """빈 컨테이너를 잡은 것이다. 빈 값을 원문이라고 넣으면 분류가 그것을 읽고 아무것도 못 낸다."""
    await run_once(conn, target(), collectors=collectors(StubDetail(source="\n\n   ")), limit=1)

    assert set(stored(conn)) == RECORD_KEYS


async def test_원문이_없는_건도_정규화까지_간다(conn: sqlite3.Connection) -> None:
    """적재만 되고 정규화에서 걸리면 소비 측에는 없는 것과 같다."""
    await run_once(conn, target(), collectors=collectors(StubDetail()), limit=1)

    rows = conn.execute("SELECT title FROM normalized_jobs").fetchall()
    assert len(rows) == 1


async def test_원문이_있는_건도_정규화까지_간다(conn: sqlite3.Connection) -> None:
    """새 키 하나가 늘었다고 정규화가 걸리면, 수집이 되는 것은 아무 뜻이 없다."""
    await run_once(
        conn,
        target(),
        collectors=collectors(StubDetail(source="제목\n회사 예시\n본문이다")),
        limit=1,
    )

    rows = conn.execute("SELECT title FROM normalized_jobs").fetchall()
    assert len(rows) == 1
