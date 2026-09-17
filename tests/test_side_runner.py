"""부가 워크플로우 실행기. 실행 하나가 행 하나를 남기는지 본다.

모델에도 실사이트에도 나가지 않는다. 임시 DB 에 실제 마이그레이션을 적용하고, 분류는 가짜
제공자로 돈다.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app import db
from app.classify.batch import ClassifyProgress, ClassifyRun
from app.config import Settings
from app.side import runner, runs, store
from tests.test_classify_run import GOOD, settings_with_key
from tests.test_classify_run import _seed as seed
from tests.test_selector_generator import FakeClient


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "side.db")
    db.migrate_up(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def workflow(conn: sqlite3.Connection) -> store.SideWorkflow:
    return store.create(conn, kind="classify", name="분류")


def test_a_run_that_finishes_is_closed_as_success(
    conn: sqlite3.Connection, workflow: store.SideWorkflow
) -> None:
    def body(
        _: sqlite3.Connection, __: store.SideWorkflow, counts: runs.SideRunCounts
    ) -> str | None:
        counts.target_count = 5
        counts.processed_count = 5
        return None

    run = runner.run_once(conn, workflow.id, body)

    assert run.status == runs.SUCCESS
    assert run.finished_at is not None
    assert (run.target_count, run.processed_count) == (5, 5)
    assert run.error_message is None


def test_a_body_that_raises_still_closes_the_row(
    conn: sqlite3.Connection, workflow: store.SideWorkflow
) -> None:
    """예외로 끝난 실행이 열린 채로 남으면 화면은 그것을 영원히 진행 중으로 읽는다."""

    def body(
        _: sqlite3.Connection, __: store.SideWorkflow, counts: runs.SideRunCounts
    ) -> str | None:
        counts.target_count = 3
        raise RuntimeError("본문이 터졌다")

    run = runner.run_once(conn, workflow.id, body)

    assert run.status == runs.FAILED
    assert run.finished_at is not None
    # 터지기 전까지 채운 카운트는 남는다
    assert run.target_count == 3
    assert run.error_message == "RuntimeError: 본문이 터졌다"


def test_an_exception_does_not_reach_the_caller(
    conn: sqlite3.Connection, workflow: store.SideWorkflow
) -> None:
    """스케줄러까지 올라간 예외는 아무도 보지 못한 채 잡을 죽인다 (3.4)."""

    def body(_: sqlite3.Connection, __: store.SideWorkflow, ___: runs.SideRunCounts) -> str | None:
        raise ValueError("사유")

    # 예외가 올라오면 이 줄에서 테스트가 깨진다
    assert runner.run_once(conn, workflow.id, body).status == runs.FAILED


def test_a_body_that_reports_a_reason_closes_as_failed(
    conn: sqlite3.Connection, workflow: store.SideWorkflow
) -> None:
    """예외 없이 실패하는 실행이 있다. 키가 없는 분류가 그것이다 (3.4)."""

    def body(
        _: sqlite3.Connection, __: store.SideWorkflow, counts: runs.SideRunCounts
    ) -> str | None:
        counts.target_count = 2
        counts.failed_count = 2
        return "GEMINI_API_KEY 가 없다"

    run = runner.run_once(conn, workflow.id, body)

    assert run.status == runs.FAILED
    assert run.error_message == "GEMINI_API_KEY 가 없다"
    assert run.failed_count == 2


def test_the_settings_come_from_the_table_not_the_caller(
    conn: sqlite3.Connection, workflow: store.SideWorkflow
) -> None:
    """화면에서 범위를 좁혀 저장했는데 옛 범위로 도는 일이 없어야 한다."""
    store.update(
        conn,
        workflow.id,
        name="분류",
        status="active",
        trigger_kind="interval",
        interval_minutes=60,
        target_scope="recent",
        target_days=3,
        batch_limit=7,
    )
    seen: list[tuple[str, int | None, int]] = []

    def body(
        _: sqlite3.Connection, given: store.SideWorkflow, __: runs.SideRunCounts
    ) -> str | None:
        seen.append((given.target_scope, given.target_days, given.batch_limit))
        return None

    runner.run_once(conn, workflow.id, body)

    assert seen == [("recent", 3, 7)]


def test_running_a_workflow_that_is_gone_is_refused(conn: sqlite3.Connection) -> None:
    with pytest.raises(store.SideWorkflowNotFoundError):
        runner.run_once(conn, 404, lambda *_: None)


def test_the_run_is_recorded_before_the_body_runs(
    conn: sqlite3.Connection, workflow: store.SideWorkflow
) -> None:
    """도는 동안 행이 열려 있어야 화면이 진행 중을 읽는다."""
    open_rows: list[int] = []

    def body(
        inner: sqlite3.Connection, __: store.SideWorkflow, ___: runs.SideRunCounts
    ) -> str | None:
        row = inner.execute("SELECT count(*) AS n FROM side_runs WHERE status IS NULL").fetchone()
        open_rows.append(int(row["n"]))
        return None

    runner.run_once(conn, workflow.id, body)

    assert open_rows == [1]
    # 마지막 실행 시각은 시작에 적힌다 (Push 1 결정)
    updated = store.read(conn, workflow.id)
    assert updated is not None and updated.last_run_at is not None


@pytest.fixture
def jobs(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    seed(connection)
    try:
        yield connection
    finally:
        connection.close()


def test_the_classify_kind_runs_the_chosen_scope(jobs: sqlite3.Connection) -> None:
    """대상·처리 건수가 그대로 `side_runs` 에 들어간다 (3.2.V)."""
    workflow = store.create(jobs, kind="classify", name="미분류 분류")

    run = runner.run_now(
        jobs, workflow.id, client=FakeClient(GOOD, GOOD, GOOD), settings=settings_with_key()
    )

    assert run.status == runs.SUCCESS
    assert (run.target_count, run.processed_count, run.failed_count) == (3, 3, 0)
    stored = jobs.execute("SELECT count(*) AS n FROM job_classifications").fetchone()
    assert stored["n"] == 3


def test_the_batch_limit_cuts_the_target(jobs: sqlite3.Connection) -> None:
    """1회 상한을 넘겨 돌면 멈출 수가 없다 (PRD 2절)."""
    workflow = store.create(jobs, kind="classify", name="두 건만", batch_limit=2)

    run = runner.run_now(
        jobs, workflow.id, client=FakeClient(GOOD, GOOD), settings=settings_with_key()
    )

    assert (run.target_count, run.processed_count) == (2, 2)


def test_a_failed_posting_is_counted_and_the_rest_go_on(jobs: sqlite3.Connection) -> None:
    """한 건이 실패해도 나머지는 간다. 실패 건수는 행에 남는다.

    깨진 응답에는 한 번 더 묻는다 (`.claude/rules/llm.md`). 그래서 첫 공고를 실패시키려면
    나쁜 응답이 둘 필요하다.
    """
    workflow = store.create(jobs, kind="classify", name="분류")

    run = runner.run_now(
        jobs,
        workflow.id,
        client=FakeClient("이건 JSON 이 아니다", "이것도 아니다", GOOD, GOOD),
        settings=settings_with_key(),
    )

    assert (run.target_count, run.processed_count, run.failed_count) == (3, 2, 1)


def test_an_empty_scope_is_not_a_failure(conn: sqlite3.Connection) -> None:
    """대상이 없는 것과 실패한 것은 다르다. 다만 아무 일도 없었다는 사실은 적힌다."""
    workflow = store.create(conn, kind="classify", name="분류")

    run = runner.run_now(conn, workflow.id, settings=settings_with_key())

    assert run.status == runs.SUCCESS
    assert (run.target_count, run.processed_count) == (0, 0)
    assert run.note is not None and "대상이 없다" in run.note


def test_the_scope_stored_on_the_workflow_is_the_one_that_runs(jobs: sqlite3.Connection) -> None:
    """`all` 은 이미 분류된 건까지 다시 돈다. `unclassified` 는 그러지 않는다."""
    workflow = store.create(jobs, kind="classify", name="전량")
    runner.run_now(
        jobs, workflow.id, client=FakeClient(GOOD, GOOD, GOOD), settings=settings_with_key()
    )
    store.update(
        jobs,
        workflow.id,
        name="전량",
        status="paused",
        trigger_kind="manual",
        interval_minutes=360,
        target_scope="all",
        target_days=None,
        batch_limit=50,
    )

    again = runner.run_now(
        jobs, workflow.id, client=FakeClient(GOOD, GOOD, GOOD), settings=settings_with_key()
    )

    assert again.target_count == 3


def test_a_deliver_workflow_says_it_sends_nothing(conn: sqlite3.Connection) -> None:
    """보낸 적 없는 실행이 성공 이력으로 쌓이면 안 된다 (PRD 3절)."""
    workflow = store.create(conn, kind="deliver", name="스프링 전달")

    run = runner.run_now(conn, workflow.id)

    assert run.status == runs.FAILED
    assert run.error_message is not None and "아직 실행할 수 없다" in run.error_message


def test_a_run_is_skipped_while_the_previous_one_is_open(
    conn: sqlite3.Connection, workflow: store.SideWorkflow
) -> None:
    """앞 실행이 아직 돌고 있으면 새 실행을 시작하지 않는다 (3.3.V)."""
    runs.start(conn, workflow.id, runner.SCHEDULE)

    second = runner.run_now(conn, workflow.id, trigger=runner.SCHEDULE)

    assert second.status == runs.SKIPPED
    assert second.note is not None and "아직 돌고 있다" in second.note
    assert second.finished_at is not None


def test_the_second_call_during_a_run_is_recorded_not_dropped(
    conn: sqlite3.Connection, workflow: store.SideWorkflow
) -> None:
    """건너뛴 차례가 조용히 사라지면 주기가 도는지 알 수 없다 (PRD 2절)."""
    taken: list[runner.Claim] = []

    def body(
        inner: sqlite3.Connection, given: store.SideWorkflow, __: runs.SideRunCounts
    ) -> str | None:
        taken.append(runner.claim(inner, given.id, runner.SCHEDULE))
        return None

    first = runner.run_once(conn, workflow.id, body)

    assert first.status == runs.SUCCESS
    assert taken[0].started is False
    blocked = runs.read(conn, taken[0].run_id)
    assert blocked is not None and blocked.status == runs.SKIPPED
    # 실행 두 건이 남는다 — 돈 것 하나와 건너뛴 것 하나
    assert conn.execute("SELECT count(*) AS n FROM side_runs").fetchone()["n"] == 2


def test_a_finished_run_does_not_block_the_next_one(
    conn: sqlite3.Connection, workflow: store.SideWorkflow
) -> None:
    """막는 것은 열려 있는 실행뿐이다. 끝난 실행은 다음 차례를 막지 않는다."""
    runner.run_now(conn, workflow.id, settings=settings_with_key())

    second = runner.run_now(conn, workflow.id, settings=settings_with_key())

    assert second.status == runs.SUCCESS


def test_the_other_classify_entry_point_blocks_a_side_run(
    conn: sqlite3.Connection, workflow: store.SideWorkflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`POST /api/classify` 로 시작한 분류가 돌고 있으면 부가 실행도 물러난다."""
    running = ClassifyRun()
    running._progress = ClassifyProgress(running=True)
    monkeypatch.setattr(runner, "get_classify_run", lambda: running)

    run = runner.run_now(conn, workflow.id)

    assert run.status == runs.SKIPPED
    assert run.note is not None and "/api/classify" in run.note


def test_a_side_run_in_flight_is_visible_to_the_other_entry_point(
    conn: sqlite3.Connection, workflow: store.SideWorkflow
) -> None:
    """반대 방향. 겹침 방지가 한쪽에만 걸리면 막으나 마나다."""
    assert runner.classify_running(conn) is None

    runs.start(conn, workflow.id, runner.MANUAL)

    reason = runner.classify_running(conn)
    assert reason is not None and str(workflow.id) in reason


def test_a_deliver_run_does_not_block_classification(conn: sqlite3.Connection) -> None:
    """막는 것은 같은 일을 하는 실행뿐이다. 전달은 분류와 다른 일이다."""
    deliver = store.create(conn, kind="deliver", name="스프링 전달")
    runs.start(conn, deliver.id, runner.MANUAL)

    assert runner.classify_running(conn) is None


def test_a_run_without_a_provider_key_closes_as_failed(jobs: sqlite3.Connection) -> None:
    """호출을 한 번도 못 한 실행이 성공으로 닫히면 안 된다 (3.4.V).

    실제 모델은 부르지 않는다. 키가 없다는 것을 `app/classify/batch.py` 가 예외 없이 사유로
    돌려주는 경로다.
    """
    workflow = store.create(jobs, kind="classify", name="분류")

    run = runner.run_now(jobs, workflow.id, settings=Settings(gemini_api_key=""))

    assert run.status == runs.FAILED
    assert run.finished_at is not None
    assert run.error_message is not None and "API 키가 없다" in run.error_message
    assert (run.target_count, run.processed_count) == (3, 0)
    # 아무것도 쓰지 않았다
    assert jobs.execute("SELECT count(*) AS n FROM job_classifications").fetchone()["n"] == 0


def test_a_partly_failed_run_is_still_a_success(jobs: sqlite3.Connection) -> None:
    """나머지가 실제로 분류됐다. 실패한 건은 다음 실행이 다시 집어 든다."""
    workflow = store.create(jobs, kind="classify", name="분류")

    run = runner.run_now(
        jobs,
        workflow.id,
        client=FakeClient("깨진 응답", "또 깨진 응답", GOOD, GOOD),
        settings=settings_with_key(),
    )

    assert run.status == runs.SUCCESS
    assert (run.processed_count, run.failed_count) == (2, 1)
    assert run.note is not None and "1건 실패" in run.note


def test_a_third_call_is_still_blocked_after_a_skip(
    conn: sqlite3.Connection, workflow: store.SideWorkflow
) -> None:
    """건너뛴 행이 마지막 실행이 되어도 막는 것은 열려 있는 실행이다 (3.3.2).

    건너뜀 행은 열자마자 닫힌다. "마지막 행이 돌고 있는가" 로 보면 그 행이 앞의 열린 실행을
    가려서, 두 번째 차례부터는 겹쳐 도는 것을 허용하게 된다.
    """
    runs.start(conn, workflow.id, runner.SCHEDULE)

    first = runner.run_now(conn, workflow.id, trigger=runner.SCHEDULE)
    second = runner.run_now(conn, workflow.id, trigger=runner.SCHEDULE)

    assert first.status == runs.SKIPPED
    assert second.status == runs.SKIPPED
