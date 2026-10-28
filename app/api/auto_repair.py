"""셀렉터 자동 수리. 수집이 셀렉터 때문에 실패하면 AI 로 고치고 시험 수집으로 확인한다.

2026-10-21 결정.

전에는 운영자가 사이트 줄을 열어 `AI로 고치기` → `셀렉터 저장` → `테스트 다시 실행` 을 차례로 눌렀다
(`app/api/ui_tests.py`). 고치기(`crawlers.get_repairer`)도 시험 수집(`crawlers.test_run`)도 이미
있는 경로라, 여기서는 그 둘을 잇기만 한다. 화면 전용 경로를 따로 만들지 않는다.

1. 저장된 셀렉터에서 실패한 칸만 AI 가 다시 고른다
2. 바뀐 것이 있으면 저장한다. AI 가 렌더한 HTML 로 고쳤으면 수집 모드도 렌더로 옮긴다
3. 시험 수집(공고 3건)을 돈다. 되면 남기고, 안 되면 원래 셀렉터와 수집 모드로 되돌린다

## 언제 도는가

- 사이트 추가의 시험 수집이 실패했을 때 (`app/api/ui_site_add.py`). 한 번 고쳐 보고, 되면 자동
  수집까지 시작한다
- 매일 수집이 셀렉터 문제(`REPAIRABLE`)로 실패했을 때 (`app/scheduler.py`). 크롤러마다 하루 한
  번이다 — 고쳐지지 않는 사이트에 매번 돈을 쓰지 않는다. 고쳐지면 그 자리에서 한 번 더 수집한다

접속 실패·시간 초과는 셀렉터가 아니라 사이트 문제라 고치지 않는다. 상세로 가는 길을 못 찾은
경우(`detail_unreachable`)도 셀렉터 고치기로는 풀리지 않는다.

## 운영자 모르게 바뀐다

그래서 무엇을 무엇으로 바꿨는지 `selector_auto_repairs` 에 남긴다 (`migrations/0058`). 고쳐도
시험 수집이 안 되면 저장하지 않은 것과 같게 되돌린다 — 실패한 셀렉터가 실패한 다른 셀렉터로
바뀌어 남지 않는다.

## 상세 예시 주소

고치기는 목록 주소와 상세 예시 주소를 다시 가져와 본다. 등록 때 받은 예시 공고는 마감되면
사라지므로, 매일 수집에서는 이번 실행이 상세에서 실패한 공고 주소를 먼저 쓰고, 없으면 이
사이트가 가장 최근에 모은 공고 주소를 쓴다.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field

from fastapi import HTTPException

from app.api import crawlers
from app.crawler.failures import FAILED, SUCCESS
from app.crawler.fetcher import FetchError, FetchPolicy
from app.crawler.playwright import PLAYWRIGHT, STATIC
from app.crawler.runner import RunResult
from app.llm.log import SELECTOR_REPAIR, record_call
from app.selector.generator import SelectorGenerationError
from app.selector.repair import SelectorRepairError
from app.selector.schema import SelectorSchemaError, validate_selectors

logger = logging.getLogger(__name__)

REGISTER = "register"
SCHEDULE = "schedule"

REPAIRED = "repaired"
REVERTED = "reverted"
NO_CHANGE = "no_change"
ERROR = "error"

# 셀렉터를 고치면 풀릴 수 있는 실패 (`app/crawler/failures.py`)
REPAIRABLE: tuple[str, ...] = ("selector_miss", "list_empty", "parse", "detail_empty")

# 고친 뒤 시험 수집에서 상세까지 따라갈 공고 수. 사이트 추가의 시험 수집과 같다
TRY_LIMIT = 3


@dataclass(frozen=True)
class AutoRepair:
    """자동 수리 한 번의 결과. 사이트 추가 창이 단계 기록으로 옮긴다."""

    outcome: str
    message: str
    changes: list[str] = field(default_factory=list)
    test: crawlers.TestRunOut | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == REPAIRED


async def repair_and_test(
    conn: sqlite3.Connection,
    crawler_id: int,
    *,
    trigger: str,
    repair: crawlers.RepairFn,
    fetcher: FetchPolicy,
    error_class: str | None = None,
    detail_url: str = "",
) -> AutoRepair:
    """고치고, 저장하고, 시험 수집으로 확인한다. 안 되면 되돌린다. 예외를 올리지 않는다.

    `detail_url` 을 주면 저장된 예시 공고 대신 그 주소로 상세 칸을 본다.
    """
    row = conn.execute(
        "SELECT list_url, detail_url, selectors_json, list_mode, detail_mode"
        " FROM crawlers WHERE id = ?",
        (crawler_id,),
    ).fetchone()
    if row is None or not row["selectors_json"]:
        return AutoRepair(ERROR, "셀렉터가 없는 크롤러다")
    before_json = str(row["selectors_json"])
    list_mode, detail_mode = str(row["list_mode"]), str(row["detail_mode"])
    note = _Log(conn, crawler_id, trigger, error_class, before_json, list_mode)
    if list_mode not in (STATIC, PLAYWRIGHT):
        # 목록이 API 인 사이트는 셀렉터가 아니라 API 설정으로 읽는다
        return note(
            AutoRepair(ERROR, f"목록을 {list_mode} 로 읽는 사이트라 셀렉터로 고치지 않는다")
        )
    try:
        selectors = validate_selectors(json.loads(before_json))
    except (json.JSONDecodeError, SelectorSchemaError) as exc:
        return note(AutoRepair(ERROR, f"저장된 셀렉터를 읽을 수 없다: {exc}"))

    try:
        outcome = await repair(
            str(row["list_url"]),
            detail_url or str(row["detail_url"] or ""),
            list_mode,
            selectors,
        )
    except SelectorRepairError as exc:
        kind = NO_CHANGE if exc.reason == "nothing_to_repair" else ERROR
        return note(AutoRepair(kind, f"AI 가 고치지 못했다: {exc}"))
    except (SelectorGenerationError, FetchError) as exc:
        return note(AutoRepair(ERROR, f"AI 로 고치기를 돌리지 못했다: {exc}"))
    for usage in (*outcome.earlier_usages, outcome.usage):
        record_call(conn, feature=SELECTOR_REPAIR, usage=usage)

    new_mode = outcome.render_mode or list_mode
    if not outcome.changes and new_mode == list_mode:
        return note(AutoRepair(NO_CHANGE, "AI 가 바꾼 셀렉터가 없다"))
    changes = [change.name for change in outcome.changes]
    after_json = outcome.selectors.to_json()
    # 렌더로 고쳤으면 목록과 상세를 함께 옮긴다. 화면의 저장과 같다 (`crawlers.update_render_mode`)
    after_detail_mode = new_mode if new_mode != list_mode else detail_mode
    _write(conn, crawler_id, after_json, new_mode, after_detail_mode)

    try:
        test = await crawlers.test_run(crawler_id, conn, fetcher, TRY_LIMIT, "")
    except HTTPException as exc:
        _write(conn, crawler_id, before_json, list_mode, detail_mode)
        message = str(exc.detail.get("message", "")) if isinstance(exc.detail, dict) else ""
        return note(
            AutoRepair(ERROR, f"고친 셀렉터로 시험 수집을 돌리지 못했다: {message}", changes),
            after_json=after_json,
            after_mode=new_mode,
        )

    passed = test.status == SUCCESS and test.success_count > 0
    if not passed:
        _write(conn, crawler_id, before_json, list_mode, detail_mode)
    result = AutoRepair(
        REPAIRED if passed else REVERTED,
        (
            f"고친 셀렉터로 공고 {test.success_count}건을 가져왔다"
            if passed
            else f"고쳐도 시험 수집이 안 돼 되돌렸다: {test.error_message or test.status}"
        ),
        changes,
        test,
    )
    return note(result, after_json=after_json, after_mode=new_mode, test_run_id=test.run_id)


async def after_failed_run(
    conn: sqlite3.Connection,
    workflow_id: int,
    result: RunResult,
    *,
    repair: crawlers.RepairFn | None = None,
    fetcher: FetchPolicy | None = None,
) -> bool:
    """매일 수집이 셀렉터 문제로 실패했으면 고쳐 본다. 고쳐졌으면 True — 부르는 쪽이 다시 돈다."""
    if result.status != FAILED or result.error_class not in REPAIRABLE:
        return False
    row = conn.execute("SELECT crawler_id FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
    if row is None:
        return False
    crawler_id = int(row["crawler_id"])
    if repaired_today(conn, crawler_id):
        logger.info("자동 수리: 크롤러 %s 는 오늘 이미 고쳐 봤다", crawler_id)
        return False
    fixed = await repair_and_test(
        conn,
        crawler_id,
        trigger=SCHEDULE,
        repair=repair or crawlers.get_repairer(conn),
        fetcher=fetcher or crawlers.get_crawl_fetcher(),
        error_class=result.error_class,
        detail_url=_recent_detail_url(conn, workflow_id, result),
    )
    logger.info("자동 수리: 크롤러 %s %s — %s", crawler_id, fixed.outcome, fixed.message)
    return fixed.ok


def repaired_today(conn: sqlite3.Connection, crawler_id: int) -> bool:
    """오늘 매일 수집에서 이미 고쳐 봤는가. 날짜는 DB 시각(UTC) 기준이다."""
    row = conn.execute(
        "SELECT 1 FROM selector_auto_repairs WHERE crawler_id = ? AND trigger = ?"
        " AND date(created_at) = date('now')",
        (crawler_id, SCHEDULE),
    ).fetchone()
    return row is not None


def _recent_detail_url(conn: sqlite3.Connection, workflow_id: int, result: RunResult) -> str:
    """지금 살아 있을 상세 공고 주소. 이번 실행이 상세에서 실패한 공고, 없으면 최근에 모은 공고."""
    for failure in result.failures:
        if failure.source_url:
            return failure.source_url
    row = conn.execute(
        "SELECT source_url FROM raw_jobs WHERE workflow_id = ? ORDER BY id DESC LIMIT 1",
        (workflow_id,),
    ).fetchone()
    return str(row["source_url"]) if row else ""


def _write(
    conn: sqlite3.Connection, crawler_id: int, selectors_json: str, list_mode: str, detail_mode: str
) -> None:
    conn.execute(
        "UPDATE crawlers SET selectors_json = ?, list_mode = ?, detail_mode = ? WHERE id = ?",
        (selectors_json, list_mode, detail_mode, crawler_id),
    )


@dataclass(frozen=True)
class _Log:
    """한 번의 자동 수리를 `selector_auto_repairs` 한 행으로 남긴다. 받은 결과를 그대로 돌려준다."""

    conn: sqlite3.Connection
    crawler_id: int
    trigger: str
    error_class: str | None
    before_json: str
    before_mode: str

    def __call__(
        self,
        result: AutoRepair,
        *,
        after_json: str | None = None,
        after_mode: str | None = None,
        test_run_id: int | None = None,
    ) -> AutoRepair:
        self.conn.execute(
            "INSERT INTO selector_auto_repairs (crawler_id, trigger, error_class, outcome,"
            " message, before_json, after_json, before_mode, after_mode, test_run_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.crawler_id,
                self.trigger,
                self.error_class,
                result.outcome,
                result.message,
                self.before_json,
                after_json,
                self.before_mode,
                after_mode,
                test_run_id,
            ),
        )
        return result
