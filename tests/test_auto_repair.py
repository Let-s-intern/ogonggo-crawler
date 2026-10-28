"""셀렉터 자동 수리 — 고치고, 저장하고, 시험 수집으로 확인하고, 안 되면 되돌린다 (2026-10-21).

AI 도 실사이트도 부르지 않는다. 고치기는 저장된 픽스처로 계산한 결과를 돌려주고, 시험 수집은
정해 둔 결과를 돌려준다 (`app/api/auto_repair.py`).

| 확인 | 깨지면 |
|---|---|
| 시험 수집이 되면 고친 셀렉터가 남고 기록된다 | 고쳐도 다음 수집이 옛 셀렉터로 돈다 |
| 렌더로 고쳤으면 수집 모드도 렌더로 옮긴다 | 고친 셀렉터가 정적 HTML 에 돈다 |
| 시험 수집이 안 되면 셀렉터와 모드가 되돌아간다 | 실패한 셀렉터가 다른 실패한 셀렉터로 바뀐다 |
| 바뀐 것이 없거나 고칠 것이 없으면 손대지 않는다 | 같은 셀렉터로 시험 수집만 헛돈다 |
| 매일 수집은 셀렉터 실패에만, 크롤러마다 하루 한 번 | 접속 실패에도, 매번 AI 를 부른다 |
| 매일 수집은 이번 실행이 실패한 공고 주소로 상세를 본다 | 마감돼 사라진 예시 공고를 본다 |
| 사이트 추가는 고쳐지면 자동 수집까지 시작한다 | 사람이 고치고 다시 걸어야 한다 |
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest

from app import db
from app.api import auto_repair
from app.api import crawlers as crawlers_api
from app.crawler.runner import ItemFailure, RunResult
from app.selector.repair import RepairOutcome, SelectorRepairError
from app.selector.schema import SelectorSet
from tests.test_api_selector_repair import DETAIL_URL, FIXED, LIST_URL, insert_crawler, outcome_for
from tests.test_selector_repair import BROKEN


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    try:
        yield connection
    finally:
        connection.close()


class Repairer:
    """고치기 자리. 받은 상세 주소를 적어 두고 정해 둔 결과를 돌려준다."""

    def __init__(self, make: Any = outcome_for) -> None:
        self.make = make
        self.detail_urls: list[str] = []

    async def __call__(
        self, list_url: str, detail_url: str, render_mode: str, selectors: SelectorSet, **_: Any
    ) -> RepairOutcome:
        self.detail_urls.append(detail_url)
        if isinstance(self.make, Exception):
            raise self.make
        result: RepairOutcome = self.make(selectors)
        return result


def _change() -> Any:
    from app.selector.repair import SelectorChange

    return SelectorChange(
        name="list.item", before=BROKEN["list"]["item"], after=FIXED["list"]["item"]
    )


def with_change(selectors: SelectorSet) -> RepairOutcome:
    return replace(outcome_for(selectors), changes=[_change()])


def rendered(selectors: SelectorSet) -> RepairOutcome:
    return replace(with_change(selectors), render_mode="playwright")


def run_result(*, ok: bool) -> crawlers_api.TestRunOut:
    return crawlers_api.TestRunOut(
        crawler_id=1,
        run_id=7,
        status="success" if ok else "failed",
        crawler_status="tested" if ok else "draft",
        render_mode="static",
        saved_render_mode="static",
        matched=10,
        success_count=3 if ok else 0,
        new_count=0,
        fail_count=0 if ok else 3,
        skipped_count=0,
        error_class=None if ok else "selector_miss",
        error_message="" if ok else "항목 0개",
        items=[],
        failures=[],
    )


def fake_test(monkeypatch: pytest.MonkeyPatch, *, ok: bool) -> list[str]:
    """시험 수집 자리. 돌 때 저장돼 있던 셀렉터를 적어 둔다 — 고친 것으로 돌았는지 본다."""
    seen: list[str] = []

    async def run(crawler_id: int, conn: sqlite3.Connection, *_: Any) -> Any:
        row = conn.execute(
            "SELECT selectors_json FROM crawlers WHERE id = ?", (crawler_id,)
        ).fetchone()
        seen.append(json.loads(row[0])["list"]["item"])
        return run_result(ok=ok)

    monkeypatch.setattr(crawlers_api, "test_run", run)
    return seen


def repair(
    conn: sqlite3.Connection, crawler_id: int, repairer: Repairer, trigger: str = "register"
) -> auto_repair.AutoRepair:
    return asyncio.run(
        auto_repair.repair_and_test(
            conn,
            crawler_id,
            trigger=trigger,
            repair=repairer,
            fetcher=None,  # type: ignore[arg-type]
        )
    )


def stored(conn: sqlite3.Connection, crawler_id: int) -> tuple[str, str, str]:
    row = conn.execute(
        "SELECT selectors_json, list_mode, detail_mode FROM crawlers WHERE id = ?", (crawler_id,)
    ).fetchone()
    return json.loads(row[0])["list"]["item"], row[1], row[2]


def logged(conn: sqlite3.Connection) -> list[tuple[Any, ...]]:
    return [
        tuple(row)
        for row in conn.execute(
            "SELECT trigger, outcome, before_mode, after_mode, test_run_id"
            " FROM selector_auto_repairs ORDER BY id"
        )
    ]


def test_a_repair_that_passes_the_test_run_is_kept(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    crawler_id = insert_crawler(conn)
    seen = fake_test(monkeypatch, ok=True)

    result = repair(conn, crawler_id, Repairer(with_change))

    assert result.ok
    assert result.changes == ["list.item"]
    assert seen == [FIXED["list"]["item"]]
    assert stored(conn, crawler_id) == (FIXED["list"]["item"], "static", "static")
    assert logged(conn) == [("register", "repaired", "static", "static", 7)]
    # AI 호출도 비용으로 남는다
    assert (
        conn.execute("SELECT count(*) FROM llm_calls WHERE feature = 'selector_repair'").fetchone()[
            0
        ]
        == 1
    )


def test_a_repair_made_on_rendered_html_moves_the_crawler_to_rendering(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    crawler_id = insert_crawler(conn)
    fake_test(monkeypatch, ok=True)

    repair(conn, crawler_id, Repairer(rendered))

    assert stored(conn, crawler_id) == (FIXED["list"]["item"], "playwright", "playwright")


def test_a_repair_that_fails_the_test_run_is_rolled_back(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    crawler_id = insert_crawler(conn)
    seen = fake_test(monkeypatch, ok=False)

    result = repair(conn, crawler_id, Repairer(rendered))

    assert not result.ok
    assert seen == [FIXED["list"]["item"]]
    assert stored(conn, crawler_id) == (BROKEN["list"]["item"], "static", "static")
    assert logged(conn) == [("register", "reverted", "static", "playwright", 7)]


def test_nothing_changed_leaves_the_crawler_alone(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    crawler_id = insert_crawler(conn)
    seen = fake_test(monkeypatch, ok=True)

    result = repair(conn, crawler_id, Repairer(outcome_for))

    assert result.outcome == auto_repair.NO_CHANGE
    assert seen == []
    assert stored(conn, crawler_id) == (BROKEN["list"]["item"], "static", "static")


def test_nothing_to_repair_is_not_an_error(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    crawler_id = insert_crawler(conn)
    seen = fake_test(monkeypatch, ok=True)

    result = repair(
        conn, crawler_id, Repairer(SelectorRepairError("nothing_to_repair", "실패한 칸이 없다"))
    )

    assert result.outcome == auto_repair.NO_CHANGE
    assert seen == []
    assert logged(conn) == [("register", "no_change", "static", None, None)]


# 매일 수집 ---------------------------------------------------------------------


def add_workflow(conn: sqlite3.Connection, crawler_id: int) -> int:
    cursor = conn.execute(
        "INSERT INTO workflows (crawler_id, name) VALUES (?, '그룹 채용')", (crawler_id,)
    )
    return int(cursor.lastrowid or 0)


def failed_run(error_class: str | None, failure_url: str = "") -> RunResult:
    failures = (
        [ItemFailure(source_url=failure_url, title="", error_class="detail_empty", message="빔")]
        if failure_url
        else []
    )
    return RunResult(run_id=3, status="failed", error_class=error_class, failures=failures)


def after_failed_run(
    conn: sqlite3.Connection, workflow_id: int, run: RunResult, r: Repairer
) -> bool:
    return asyncio.run(
        auto_repair.after_failed_run(conn, workflow_id, run, repair=r, fetcher=object())  # type: ignore[arg-type]
    )


def test_a_scheduled_selector_failure_is_repaired_once_a_day(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_id = add_workflow(conn, insert_crawler(conn))
    fake_test(monkeypatch, ok=False)
    repairer = Repairer(with_change)

    first = after_failed_run(conn, workflow_id, failed_run("selector_miss"), repairer)
    second = after_failed_run(conn, workflow_id, failed_run("selector_miss"), repairer)

    assert (first, second) == (False, False)
    assert len(repairer.detail_urls) == 1
    assert logged(conn) == [("schedule", "reverted", "static", "static", 7)]


def test_a_scheduled_repair_that_passes_asks_for_another_run(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_id = add_workflow(conn, insert_crawler(conn))
    fake_test(monkeypatch, ok=True)

    assert after_failed_run(conn, workflow_id, failed_run("list_empty"), Repairer(with_change))


@pytest.mark.parametrize("error_class", ["transport", "detail_unreachable", None])
def test_failures_a_selector_cannot_fix_are_left_alone(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, error_class: str | None
) -> None:
    workflow_id = add_workflow(conn, insert_crawler(conn))
    fake_test(monkeypatch, ok=True)
    repairer = Repairer(with_change)

    assert not after_failed_run(conn, workflow_id, failed_run(error_class), repairer)
    assert repairer.detail_urls == []


def test_a_successful_run_is_left_alone(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_id = add_workflow(conn, insert_crawler(conn))
    repairer = Repairer(with_change)

    ok = RunResult(run_id=3, status="success", success_count=4)

    assert not after_failed_run(conn, workflow_id, ok, repairer)
    assert repairer.detail_urls == []


def test_the_scheduled_repair_looks_at_a_live_posting(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """등록 때 받은 예시 공고는 마감되면 사라진다. 이번 실행이 실패한 공고가 먼저다."""
    workflow_id = add_workflow(conn, insert_crawler(conn))
    fake_test(monkeypatch, ok=False)
    conn.execute(
        "INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash)"
        " VALUES (?, ?, '{}', 'h')",
        (workflow_id, f"{LIST_URL}/view/9"),
    )
    failing, recent, saved = Repairer(with_change), Repairer(with_change), Repairer(with_change)

    after_failed_run(conn, workflow_id, failed_run("detail_empty", f"{LIST_URL}/view/12"), failing)
    conn.execute("DELETE FROM selector_auto_repairs")
    after_failed_run(conn, workflow_id, failed_run("detail_empty"), recent)
    conn.execute("DELETE FROM selector_auto_repairs")
    conn.execute("DELETE FROM raw_jobs")
    after_failed_run(conn, workflow_id, failed_run("detail_empty"), saved)

    assert failing.detail_urls == [f"{LIST_URL}/view/12"]
    assert recent.detail_urls == [f"{LIST_URL}/view/9"]
    assert saved.detail_urls == [DETAIL_URL]


@pytest.mark.parametrize(("fixed", "runs"), [(True, 2), (False, 1)])
def test_the_scheduler_runs_again_only_when_the_repair_worked(
    conn: sqlite3.Connection,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    fixed: bool,
    runs: int,
) -> None:
    """잡 실행 경로(`_execute`)다. 고쳐졌으면 그 자리에서 한 번 더 수집한다."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from app import scheduler as scheduler_module

    real_connect = db.connect
    monkeypatch.setattr(db, "connect", lambda path=None: real_connect(tmp_path / "jobs.db"))
    called: list[int] = []

    async def run_workflow(conn: sqlite3.Connection, workflow_id: int, **_: Any) -> RunResult:
        called.append(workflow_id)
        return failed_run("selector_miss")

    async def after(*_: Any, **__: Any) -> bool:
        return fixed

    monkeypatch.setattr(scheduler_module, "run_workflow", run_workflow)
    monkeypatch.setattr(auto_repair, "after_failed_run", after)
    instance = scheduler_module.WorkflowScheduler(scheduler=AsyncIOScheduler(timezone="UTC"))

    asyncio.run(instance._execute(5))

    assert called == [5] * runs
