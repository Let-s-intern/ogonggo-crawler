"""상시 인재 풀 등록은 채용 공고가 아니라 건너뛴다 (2026-09-17 결정).

제목은 로컬 DB 에 쌓인 LG 목록에서 그대로 가져왔다.
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator

import pytest

from app import db
from app.crawler.collect import API, Collectors
from app.crawler.parser import ListItem
from app.crawler.runner import SCHEDULE, RunTarget, run_once
from app.crawler.talent_pool import is_talent_pool
from tests.test_list_date_is_deadline import EMPTY_SELECTORS, LIST_URL, StubDetail, StubList


@pytest.mark.parametrize(
    "title",
    [
        "[본사] 상시 인재 Pool 등록",
        "[정규직] 해외 칠러서비스 기술지원 담당자 모집 _인재 Pool",
        "[비즈테크아이] 상시 인재 채용 (인재 Pool)",
        "[상시접수] LG화학 상시 인재등록 공고 (전 직무)",
        "해외 및 국내 인재 상시 DB 등록용 (`26년)",
        "[LG CNS] Global 해외 석박사 AX 인재 상시 등록 공고",
        "Talent Pool 등록",
        "상시 인재풀 모집",
    ],
)
def test_인재_풀_제목을_알아본다(title: str) -> None:
    assert is_talent_pool(title)


@pytest.mark.parametrize(
    "title",
    [
        "[SK인텔릭스] AI Engineer(AI 서비스 개발) 경력 인재 채용",
        "2026년 하반기 CJ제일제당 신입사원 모집",
        "[상시채용] 백엔드 개발자",
        "",
    ],
)
def test_채용_공고는_인재_풀로_보지_않는다(title: str) -> None:
    assert not is_talent_pool(title)


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status, list_mode, detail_mode) "
        "VALUES ('예시', ?, 'promoted', 'api', 'api')",
        (LIST_URL,),
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '예시')")
    try:
        yield connection
    finally:
        connection.close()


async def test_인재_풀은_상세를_열지_않고_건너뛴다(conn: sqlite3.Connection) -> None:
    items = [
        ListItem(
            index=0, title="[본사] 상시 인재 Pool 등록", link="https://example.test/0", date=""
        ),
        ListItem(index=1, title="백엔드 개발자 경력 채용", link="https://example.test/1", date=""),
    ]
    detail = StubDetail()
    collectors = Collectors(list_mode=API, detail_mode=API, list=StubList(items), detail=detail)

    result = await run_once(
        conn,
        RunTarget(list_url=LIST_URL, selectors=EMPTY_SELECTORS, trigger=SCHEDULE, workflow_id=1),
        collectors=collectors,
    )

    assert detail.calls == ["https://example.test/1"]
    assert result.skipped_count == 1
    assert result.new_count == 1
