"""마감·중복은 상세를 열기 전에 거른다 (2.3~2.5).

실사이트에 나가지 않는다. 목록과 상세는 손으로 만든 수집기가 돌려주고, 상세 수집기는 몇 번
불렸는지를 센다 — 요청을 아끼는 것이 이 기능의 목적이라 "적재하지 않았다" 만으로는 확인이
되지 않는다.

판정은 한쪽으로만 기운다. 지났다고 확실히 읽은 것만 마감이고 나머지는 진행 중이다. 날짜 형식이
바뀐 사이트를 조용히 전부 버리는 것이 이 기능의 유일한 위험이다 (`app/crawler/deadline.py`).
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app import db
from app.crawler import deadline as deadline_module
from app.crawler.collect import Collectors, list_date_is_deadline
from app.crawler.deadline import is_closed, today
from app.crawler.parser import DetailParseResult, ListItem, ListParseResult
from app.crawler.runner import SCHEDULE, RunTarget, run_once
from app.normalize.rules import Rule, build_rule
from app.selector.api_schema import ApiConfig
from app.selector.schema import DETAIL_FIELDS, SelectorSet, validate_selectors

LIST_URL = "https://example.test/jobs"
TODAY = date(2026, 8, 25)

# 상세에 마감일이 없는 크롤러다. 이런 크롤러에서만 목록 날짜가 그대로 마감일이 된다
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

# 상세가 마감일을 주는 크롤러. 목록 날짜는 게시일일 수 있다
WITH_DETAIL_DEADLINE = validate_selectors(
    {
        "list": {"item": "ul.jobs > li", "title": "h3", "link": "a", "date": "span.d"},
        "detail": {
            "title": "p.title",
            "body": "div.body",
            "qualifications": "",
            "recruitment_end_at": "span.due",
            "department": "",
        },
    }
)


def rules() -> list[Rule]:
    """운영 DB 에 있는 것과 같은 모양의 마감일 규칙. 표기를 먼저 걸러내고 날짜로 읽는다."""
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


# 마감 판정 (2.3.V) --------------------


@pytest.mark.parametrize(
    ("value", "closed", "why"),
    [
        ("2026-08-24", True, "어제 마감된 공고다"),
        ("2026.08.20", True, "규칙이 아는 다른 표기도 읽는다"),
        ("2026-08-25", False, "오늘 마감인 공고는 아직 진행 중이다"),
        ("2026-12-31", False, "미래다"),
        ("", False, "마감일이 없는 상시채용이다"),
        ("   ", False, "빈 값과 같다"),
        ("채용시까지", False, "규칙이 날짜로 읽지 못했다. 못 읽은 것은 지난 것이 아니다"),
        ("2026년 8월 20일", False, "규칙에 없는 표기다. 형식이 바뀐 사이트를 버리지 않는다"),
        ("상시채용", False, "규칙이 값을 비웠다. 마감일이 없는 것이다"),
    ],
)
def test_마감_판정(value: str, closed: bool, why: str) -> None:
    assert is_closed(value, rules(), on=TODAY) is closed, why


def test_규칙이_없으면_읽을_수_있는_값만_판정한다() -> None:
    """정규화 규칙이 하나도 없는 설치에서도 이미 날짜 모양인 값은 그대로 읽힌다."""
    assert is_closed("2026-08-24", [], on=TODAY) is True
    assert is_closed("2026-08-26", [], on=TODAY) is False
    assert is_closed("2026.08.24", [], on=TODAY) is False


def test_오늘은_표시_시간대의_날짜다(monkeypatch: pytest.MonkeyPatch) -> None:
    """UTC 로 재면 한국 시각과 하루가 어긋난다. 검수 화면과 같은 기준을 써야 한다.

    여기서 부르는 `today` 는 모듈에서 직접 가져온 진짜 함수다. 아래 실행 테스트가 오늘을
    고정하는 것과 달리, 이 테스트가 보는 것은 그 오늘이 무엇으로 정해지는가다.
    """
    seen: set[date] = set()
    for name in ("Pacific/Kiritimati", "Asia/Seoul", "Pacific/Midway"):
        zone = ZoneInfo(name)
        monkeypatch.setattr(deadline_module, "display_zone", lambda zone=zone: zone)
        assert today() == datetime.now(zone).date()
        seen.add(today())
    # 25시간 떨어진 두 시간대라 어느 순간에도 날짜가 갈린다. 시간대를 실제로 읽는다는 뜻이다
    assert len(seen) > 1


# 실행에서 거르기 (2.3.V, 2.4.V, 2.5.V) --------------------


class StubList:
    def __init__(self, items: list[ListItem]) -> None:
        self._items = items

    async def collect(self) -> ListParseResult:
        return ListParseResult(matched=len(self._items), items=list(self._items), failures=[])


class StubDetail:
    """상세 한 건. 몇 번 불렸는지 센다."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def collect(self, item: ListItem) -> DetailParseResult:
        self.calls.append(item.link)
        fields = dict.fromkeys(DETAIL_FIELDS, "")
        fields["title"] = item.title
        fields["body"] = f"{item.title} 본문"
        return DetailParseResult(fields=fields, missing=[])


def collectors(
    items: list[ListItem], detail: StubDetail, selectors: SelectorSet = SELECTORS
) -> Collectors:
    """목록 날짜를 마감일로 볼지는 실제 판정 함수가 정한다 (`app/crawler/collect.py`)."""
    return Collectors(
        list_mode="static",
        detail_mode="static",
        list=StubList(items),
        detail=detail,
        list_date_is_deadline=list_date_is_deadline("static", ApiConfig(), selectors),
    )


def item(index: int, title: str, item_date: str) -> ListItem:
    return ListItem(index=index, title=title, link=f"{LIST_URL}/{index}", date=item_date)


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES (?, ?, 'draft')",
        ("예시", LIST_URL),
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '예시 채용')")
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
    """실행 시각으로 결과가 바뀌지 않게 오늘을 고정한다."""
    monkeypatch.setattr(deadline_module, "today", lambda: TODAY)


def target(selectors: Any = SELECTORS) -> RunTarget:
    return RunTarget(list_url=LIST_URL, selectors=selectors, trigger=SCHEDULE, workflow_id=1)


async def test_마감이_지난_항목은_상세를_열지_않는다(conn: sqlite3.Connection) -> None:
    detail = StubDetail()
    items = [
        item(0, "지난 공고", "2026-08-24"),
        item(1, "오늘 마감", "2026-08-25"),
        item(2, "진행 중", "2026-09-30"),
    ]

    result = await run_once(conn, target(), collectors=collectors(items, detail))

    # 지난 공고 하나만 빠진다. 오늘 마감은 아직 진행 중이다
    assert detail.calls == [f"{LIST_URL}/1", f"{LIST_URL}/2"]
    assert (result.skipped_count, result.fail_count) == (1, 0)
    assert result.new_count == 2
    assert result.status == "success"


async def test_읽지_못한_날짜는_상세를_연다(conn: sqlite3.Connection) -> None:
    """형식이 바뀐 사이트를 조용히 전부 버리지 않는다."""
    detail = StubDetail()
    items = [item(0, "표기가 바뀐 공고", "2026년 8월 1일"), item(1, "상시채용", "상시채용")]

    result = await run_once(conn, target(), collectors=collectors(items, detail))

    assert len(detail.calls) == 2
    assert (result.skipped_count, result.new_count) == (0, 2)


async def test_상세가_마감일을_주면_목록_날짜로_거르지_않는다(
    conn: sqlite3.Connection,
) -> None:
    """`list.date` 가 게시일인 사이트가 있다. 그것을 마감으로 읽으면 새 공고를 버린다."""
    detail = StubDetail()
    items = [item(0, "어제 올라온 공고", "2026-08-24")]

    result = await run_once(
        conn,
        target(WITH_DETAIL_DEADLINE),
        collectors=collectors(items, detail, WITH_DETAIL_DEADLINE),
    )

    assert detail.calls == [f"{LIST_URL}/0"]
    assert (result.skipped_count, result.new_count) == (0, 1)


async def test_전부_마감이어도_실행은_실패가_아니다(conn: sqlite3.Connection) -> None:
    """마감된 공고만 남은 사이트는 정상이다. 실패로 세면 자동 중지에 걸린다."""
    detail = StubDetail()
    items = [item(0, "지난 공고", "2026-08-01"), item(1, "지난 공고 2", "2026-08-24")]

    result = await run_once(conn, target(), collectors=collectors(items, detail))

    assert detail.calls == []
    assert result.status == "success"
    assert (result.skipped_count, result.fail_count, result.new_count) == (2, 0, 0)


# 이미 저장한 공고 (2.4.V) --------------------


async def test_이미_저장한_공고는_상세를_다시_열지_않는다(conn: sqlite3.Connection) -> None:
    """`_is_known()` 은 상세 요청 앞에 있어야 한다. 뒤에 있으면 요청은 이미 나간 뒤다."""
    detail = StubDetail()
    items = [item(0, "백엔드 개발자", "2026-09-30"), item(1, "프론트엔드 개발자", "")]

    first = await run_once(conn, target(), collectors=collectors(items, detail))

    assert len(detail.calls) == 2
    assert (first.new_count, first.skipped_count) == (2, 0)

    second = await run_once(conn, target(), collectors=collectors(items, detail))

    # 두 번째 실행은 상세를 한 번도 부르지 않는다. 늘어난 호출이 없다
    assert len(detail.calls) == 2
    assert (second.new_count, second.fail_count) == (0, 0)
    assert second.status == "success"
    assert len(conn.execute("SELECT id FROM raw_jobs").fetchall()) == 2


# 건너뛴 수를 실행 기록에 (2.5.V) --------------------


async def test_마감과_기존_공고를_합쳐_건너뛴_수로_남긴다(conn: sqlite3.Connection) -> None:
    """마감 2건·기존 3건이면 건너뜀 5, 실패 0 이다. 둘을 합치면 고칠 것과 정상이 섞인다."""
    detail = StubDetail()
    known = [item(index, f"기존 공고 {index}", "2026-09-30") for index in range(3)]
    await run_once(conn, target(), collectors=collectors(known, detail))
    assert len(detail.calls) == 3

    expired = [
        item(3, "지난 공고", "2026-08-24"),
        item(4, "지난 공고 2", "2026-01-01"),
    ]
    result = await run_once(conn, target(), collectors=collectors(known + expired, detail))

    assert (result.skipped_count, result.fail_count, result.new_count) == (5, 0, 0)
    assert result.status == "success"
    # 다섯 건 모두 상세 요청이 나가지 않았다
    assert len(detail.calls) == 3

    row = conn.execute(
        "SELECT skipped_count, fail_count, new_count, status FROM crawl_runs WHERE id = ?",
        (result.run_id,),
    ).fetchone()
    assert (row["skipped_count"], row["fail_count"], row["new_count"]) == (5, 0, 0)
    assert row["status"] == "success"


async def test_건너뜀과_실패는_같은_실행에서도_섞이지_않는다(conn: sqlite3.Connection) -> None:
    detail = StubDetail()
    stored = [item(0, "기존 공고", "2026-09-30")]
    await run_once(conn, target(), collectors=collectors(stored, detail))

    items = [
        *stored,
        item(1, "지난 공고", "2026-08-24"),
        ListItem(index=2, title="상세가 없는 공고", link=LIST_URL, date="", detail_absent=True),
        item(3, "새 공고", "2026-09-30"),
    ]
    result = await run_once(conn, target(), collectors=collectors(items, detail))

    assert (result.skipped_count, result.fail_count, result.new_count) == (2, 1, 1)
    row = conn.execute(
        "SELECT skipped_count, fail_count FROM crawl_runs WHERE id = ?", (result.run_id,)
    ).fetchone()
    assert (row["skipped_count"], row["fail_count"]) == (2, 1)
