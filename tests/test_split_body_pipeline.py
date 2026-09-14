"""0011 이 더한 열 칸을 파이프라인이 끝까지 나르는지 본다.

저장된 두산 픽스처(2026-08-26 목록·상세)를 돌려주는 스텁 fetch 클라이언트로 워크플로우를
1회 돌린다. 실사이트에 나가지 않는다.

두산을 고른 이유는 한 사이트 안에 두 경우가 다 있어서다. 자회사·인원·모집분야·지역·수행업무와
전형절차·기타사항·채용공고 기간은 이름표가 붙어 따로 오고, 고용형태·경력 구분·우대 조건은
아예 없다. **있으면 채워지고 없으면 빈다** 를 한 실행으로 확인할 수 있다.

확인하는 것은 두 자리다 — `raw_jobs.raw_data_json`(수집)과 `normalized_jobs`(정규화).
소비 측 경계는 `tests/test_api_jobs.py` 가 본다.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from app import db
from app.config import Settings
from app.crawler.fetcher import Fetcher
from app.crawler.runner import run_workflow

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
LIST_HTML = (FIXTURES / "doosan-list-20260825.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "doosan-detail-1000361539-20260826.html").read_text(encoding="utf-8")

LIST_URL = "https://career.doosan.com/dsp/sa/RecList.jsp"
ROBOTS = "User-agent: *\nDisallow:\n"

# 크롤러 31 에 저장된 셀렉터에 0011 의 새 칸을 더한 것이다. 두산이 주지 않는 `employment_type`,
# `experience_type`, `preferred_qualifications` 는 빈 문자열로 둔다 — 아무 요소나 억지로 고르지
# 않는다
SELECTORS: dict[str, Any] = {
    "list": {
        "item": "ul.list-cont > li",
        "title": "a.list-tit > strong",
        "link": "a.list-tit",
        "date": "div.deadline",
        "company_name": "div.company",
        "link_template": (
            "https://career.doosan.com/dsp/sa/RecList.jsp"
            "?REC_ID={onclick|arg1}&REC_TYPE_CD={onclick|arg3}&q_REC_TYPE="
            "&REC_MGT_CD={onclick|arg2}&OPEN_YN=&q_CHRG_ID=&q_SCHFIRM_ID="
            "&BA_STATUS_CD=&q_COMP_CD={onclick|arg4}&PRE_URL=REC&MENU_ID=RecList&mode=goDetail"
        ),
    },
    "detail": {
        "title": "h2.h2-title",
        "body": "div.view-list-wrap",
        "qualifications": 'th:-soup-contains("자격요건") + td',
        "recruitment_end_at": 'th:-soup-contains("진행상태") + td',
        "department": "",
        "company_name": 'dt:-soup-contains("자회사/BG") + dd',
        "recruitment_start_at": 'th:-soup-contains("채용공고") + td',
        "job_category": 'dt:-soup-contains("모집분야") + dd',
        "employment_type": "",
        "experience_type": "",
        "region": 'dt:-soup-contains("지역") + dd',
        "headcount": 'dt:-soup-contains("인원") + dd',
        "responsibilities": 'dt:-soup-contains("수행업무") + dd',
        "preferred_qualifications": "",
        "hiring_process": 'th:-soup-contains("전형절차") + td',
        "recruitment_notice": 'th:-soup-contains("기타사항") + td',
    },
}

# 두산이 그 값을 주는 칸과 이 공고에서 나와야 하는 값
FILLED = {
    "region": "서울",
}

# 두산이 주지 않는 칸. **빈 칸이어야 한다** — 다른 값으로 채우지 않는다
EMPTY = ("department", "employment_type", "experience_type", "preferred_qualifications")

# 그중 `normalized_jobs` 에 아직 남아 있는 칸. `department` 는 0016 이 지웠고, 수집은 여전히
# 그 셀렉터를 들고 있다 — `raw_jobs` 는 건드리지 않는다
# (`migrations/0016_drop_department_category_headcount.sql`)
EMPTY_NORMALIZED = ("employment_type", "experience_type", "preferred_qualifications")


def stub_fetcher() -> Fetcher:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        # 목록과 상세가 같은 경로다. 상세만 쿼리에 REC_ID 를 달고 온다
        if "REC_ID" in request.url.query.decode():
            return httpx.Response(200, text=DETAIL_HTML)
        return httpx.Response(200, text=LIST_HTML)

    async def no_wait(seconds: float) -> None:
        return None

    return Fetcher(
        settings=Settings(crawl_delay_seconds=0.0, crawl_max_retries=0),
        transport=httpx.MockTransport(handle),
        sleep=no_wait,
    )


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        """
        INSERT INTO crawlers (name, list_url, selectors_json, status)
        VALUES (?, ?, ?, 'promoted')
        """,
        ("두산", LIST_URL, json.dumps(SELECTORS)),
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '두산')")
    try:
        yield connection
    finally:
        connection.close()


async def run_once(connection: sqlite3.Connection) -> dict[str, Any]:
    """한 건만 돌리고 그 건의 `raw_data_json` 을 돌려준다."""
    await run_workflow(connection, 1, fetcher=stub_fetcher(), limit=1)
    row = connection.execute("SELECT raw_data_json FROM raw_jobs ORDER BY id LIMIT 1").fetchone()
    loaded: dict[str, Any] = json.loads(row["raw_data_json"])
    return loaded


async def test_collection_carries_the_new_columns(conn: sqlite3.Connection) -> None:
    """`_record()` 가 상세에서 읽은 값을 그대로 싣는다."""
    record = await run_once(conn)

    for name, value in FILLED.items():
        assert record[name] == value, name
    assert "■주요업무" in record["responsibilities"]
    assert record["recruitment_start_at"].startswith("2026-07-15")
    assert "서류전형" in record["hiring_process"]
    assert record["recruitment_notice"]


async def test_collection_leaves_a_column_the_site_does_not_give_empty(
    conn: sqlite3.Connection,
) -> None:
    """없는 값을 다른 값으로 채우지 않는다. 한화 `department` 에 근무지가 들어간 실수다."""
    record = await run_once(conn)

    assert [record[name] for name in EMPTY] == ["", "", "", ""]
    # 빈 칸을 본문으로 메우지 않았는지까지 본다. 본문 전체가 칸마다 반복되면 그것도 억지로
    # 채운 것이다
    assert record["responsibilities"] != record["body"]


async def test_normalization_writes_the_new_columns(conn: sqlite3.Connection) -> None:
    """정규화가 그 값을 `normalized_jobs` 의 같은 이름 칸에 넣는다."""
    await run_once(conn)

    row = conn.execute("SELECT * FROM normalized_jobs ORDER BY id LIMIT 1").fetchone()
    for name, value in FILLED.items():
        assert row[name] == value, name
    assert row["responsibilities"]
    assert row["hiring_process"]
    assert row["recruitment_notice"]
    assert row["recruitment_start_at"]


async def test_normalization_leaves_the_missing_ones_null(conn: sqlite3.Connection) -> None:
    """빈 값에는 규칙을 태우지 않고 NULL 로 둔다. 빈 문자열로 채우지 않는다."""
    await run_once(conn)

    row = conn.execute("SELECT * FROM normalized_jobs ORDER BY id LIMIT 1").fetchone()
    assert [row[name] for name in EMPTY_NORMALIZED] == [None, None, None]


async def test_a_rule_can_run_on_a_new_column(conn: sqlite3.Connection) -> None:
    """새 칸도 규칙이 만드는 필드다. 규칙 화면의 목록과 같은 값이어야 한다."""
    conn.execute(
        """
        INSERT INTO normalization_rules (field_name, rule_type, rule_config_json)
        VALUES ('region', 'regex', ?)
        """,
        (json.dumps({"pattern": r"울$", "replacement": "울시"}),),
    )

    await run_once(conn)

    row = conn.execute("SELECT region FROM normalized_jobs ORDER BY id LIMIT 1").fetchone()
    assert row["region"] == "서울시"
