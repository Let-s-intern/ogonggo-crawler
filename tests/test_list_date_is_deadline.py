"""목록 날짜가 마감일인지는 사이트마다 설정에 적는다 (4.11).

Push 2 는 상세에 마감일 셀렉터가 없는 크롤러에서만 목록 날짜로 마감을 판정했다. 목록이 API 인
크롤러는 셀렉터가 아예 없어 그 판정이 돌지 않았고, 마감이 지난 공고에도 상세 요청이 그대로
나갔다.

`list.date` 가 마감일인지 게시일인지는 사이트만 안다. 게시일을 마감일로 읽으면 어제 올라온 새
공고가 조용히 버려진다. 그래서 기본값은 "판정하지 않는다" 이고, 적은 사이트만 건너뛴다.

실사이트에 나가지 않는다. 상세 수집기는 몇 번 불렸는지를 센다 — 요청을 아끼는 것이 이 기능의
목적이라 "적재하지 않았다" 만으로는 확인이 되지 않는다.
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest

from app import db
from app.crawler import deadline as deadline_module
from app.crawler.collect import API, Collectors, list_date_is_deadline
from app.crawler.parser import DetailParseResult, ListItem, ListParseResult
from app.crawler.runner import SCHEDULE, RunTarget, run_once
from app.normalize.rules import Rule, build_rule
from app.selector.api_schema import ApiConfig, validate_api_config
from app.selector.schema import (
    DETAIL_FIELDS,
    DetailSelectors,
    ListSelectors,
    SelectorSet,
)

LIST_URL = "https://api.example.test/jobs"
TODAY = date(2026, 8, 25)

# 목록과 상세가 둘 다 API 인 크롤러는 셀렉터가 하나도 쓰이지 않는다. 러너가 이런 크롤러에
# 넘기는 것과 같은 빈 셀렉터다 (`app/crawler/runner.py`)
EMPTY_SELECTORS = SelectorSet(
    list=ListSelectors(item="", title="", link="", date=""),
    detail=DetailSelectors(
        title="", body="", qualifications="", recruitment_end_at="", department=""
    ),
)


def api_config(*, date_is_deadline: bool) -> ApiConfig:
    section: dict[str, Any] = {
        "url": LIST_URL,
        "items_path": "data.list",
        "fields": {"title": "name", "date": "endDate"},
        "id_field": "id",
        "link_template": "https://example.test/jobs/{id}",
    }
    if date_is_deadline:
        section["date_is_deadline"] = True
    return validate_api_config({"list": section})


def rules() -> list[Rule]:
    return [
        build_rule(
            "recruitment_end_at", "mapping", {"map": {"상시채용": ""}}, priority=0, rule_id=1
        ),
        build_rule(
            "recruitment_end_at",
            "date_parse",
            {"formats": ["%Y-%m-%d", "%Y.%m.%d"]},
            priority=1,
            rule_id=2,
        ),
    ]


class StubList:
    def __init__(self, items: list[ListItem]) -> None:
        self._items = items

    async def collect(self) -> ListParseResult:
        return ListParseResult(matched=len(self._items), items=self._items, failures=[])


class StubDetail:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def collect(self, item: ListItem) -> DetailParseResult:
        self.calls.append(item.link)
        fields = dict.fromkeys(DETAIL_FIELDS, "")
        fields["title"] = item.title
        fields["body"] = f"{item.title} 본문"
        return DetailParseResult(fields=fields, missing=[])


def collectors(items: list[ListItem], detail: StubDetail, config: ApiConfig) -> Collectors:
    return Collectors(
        list_mode=API,
        detail_mode=API,
        list=StubList(items),
        detail=detail,
        list_date_is_deadline=list_date_is_deadline(API, config, EMPTY_SELECTORS),
    )


def item(index: int, title: str, item_date: str) -> ListItem:
    return ListItem(
        index=index,
        title=title,
        link=f"https://example.test/jobs/{index}",
        date=item_date,
        detail_key=str(index),
    )


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status, list_mode, detail_mode) "
        "VALUES (?, ?, 'promoted', 'api', 'api')",
        ("api 예시", LIST_URL),
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, 'api 예시')")
    for rule in rules():
        connection.execute(
            """
            INSERT INTO normalization_rules (field_name, rule_type, rule_config_json, priority)
            VALUES (?, ?, ?, ?)
            """,
            (rule.field_name, rule.rule_type, rule.config_json(), rule.priority),
        )
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deadline_module, "today", lambda: TODAY)


def target() -> RunTarget:
    return RunTarget(list_url=LIST_URL, selectors=EMPTY_SELECTORS, trigger=SCHEDULE, workflow_id=1)


async def test_a_site_that_says_so_skips_the_closed_postings(conn: sqlite3.Connection) -> None:
    """마감이 지난 공고는 상세를 열지 않고 건너뜀으로 센다."""
    items = [
        item(0, "지난 공고", "2026-08-24"),
        item(1, "오늘 마감", "2026-08-25"),
        item(2, "남은 공고", "2026-09-02"),
    ]
    detail = StubDetail()

    result = await run_once(
        conn, target(), collectors=collectors(items, detail, api_config(date_is_deadline=True))
    )

    assert len(detail.calls) == 2
    assert result.skipped_count == 1
    assert result.new_count == 2
    assert result.fail_count == 0


async def test_a_site_that_stays_silent_opens_every_posting(conn: sqlite3.Connection) -> None:
    """적지 않은 크롤러는 예전 그대로다. 목록 날짜가 게시일일 수 있으므로 열어 본다."""
    items = [item(0, "지난 날짜", "2026-08-24"), item(1, "남은 공고", "2026-09-02")]
    detail = StubDetail()

    result = await run_once(
        conn, target(), collectors=collectors(items, detail, api_config(date_is_deadline=False))
    )

    assert len(detail.calls) == 2
    assert result.skipped_count == 0
    assert result.new_count == 2


async def test_a_date_the_rules_cannot_read_is_still_open(conn: sqlite3.Connection) -> None:
    """읽지 못한 날짜는 진행 중이다. 형식이 바뀐 사이트를 통째로 버리지 않는다."""
    items = [item(0, "형식이 다른 날짜", "20260824"), item(1, "상시채용", "상시채용")]
    detail = StubDetail()

    result = await run_once(
        conn, target(), collectors=collectors(items, detail, api_config(date_is_deadline=True))
    )

    assert len(detail.calls) == 2
    assert result.skipped_count == 0


def test_the_setting_is_read_from_the_list_config() -> None:
    """판정은 크롤러 설정에서 온다. 목록이 HTML 이면 상세 셀렉터가 정한다."""
    assert list_date_is_deadline(API, api_config(date_is_deadline=True), EMPTY_SELECTORS) is True
    assert list_date_is_deadline(API, api_config(date_is_deadline=False), EMPTY_SELECTORS) is False
    assert list_date_is_deadline(API, ApiConfig(), EMPTY_SELECTORS) is False
