"""부가 워크플로우 실행 1회. `side_runs` 행 하나가 실행 하나다.

`app/crawler/runner.py` 와 같은 자리의 모듈이다 — 실행 하나를 열고, 어떤 종료 경로에서도
상태와 카운트로 닫는다. 다른 점은 대상이 사이트가 아니라 이미 수집한 공고라는 것뿐이다.

## 설정은 실행할 때 표에서 다시 읽는다

`side_workflow_id` 만 받고 나머지는 `app/side/store.py` 로 읽는다. 스케줄러 메모리에 들고
있던 값으로 돌면, 화면에서 대상 범위를 좁혀 저장한 뒤에도 다음 실행이 옛 범위로 돈다.
표가 진실이라는 규칙은 크롤 워크플로우와 같다 (`.claude/rules/crawling.md`).

## 종류마다 하는 일이 다르고 기록하는 방식은 하나다

`Body` 는 그 종류가 실제로 하는 일이다. 카운트를 채우고, 실패했으면 사유 한 줄을 돌려준다.
행을 열고 닫는 일은 종류와 무관하게 여기 한 곳에서 한다 — 종류가 늘 때마다 기록을 다시 쓰면
어느 종류는 실패한 실행을 안 남기는 일이 생긴다.

## `runs.recording` 을 쓰지 않는 이유

두 가지가 맞지 않는다.

예외 없이 실패하는 실행이 있다. 제공자 키가 없으면 `app/classify/batch.py` 는 예외를 올리지
않고 진행 상황에 사유를 적고 돌아온다. 그 실행은 `failed` 로 닫히면서 사유가
`error_message` 에 남아야 하는데, `recording` 의 정상 종료 경로는 `error_message` 를 적지
않는다.

그리고 예외를 다시 올린다. 이 함수를 부르는 것은 스케줄러와 API 스레드이고, 거기까지 올라간
예외는 아무도 보지 못한 채 잡을 죽인다. 실행 하나가 실패한 것은 행에 적혀야 할 사실이지
호출자가 처리할 예외가 아니다.

`start`·`finish`·`skipped` 는 그대로 쓴다. `side_runs` 에 쓰는 문장은 그 모듈에만 있다.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.classify.batch import ClassifyProgress, classify_ids, get_classify_run
from app.classify.store import scope_ids
from app.config import Settings
from app.normalize.backfill import ConnectFactory
from app.side import runs, store
from app.side.runs import FAILED, SUCCESS, SideRun, SideRunCounts

logger = logging.getLogger(__name__)

# 무엇이 이 실행을 깨웠는가. `side_runs.trigger` 의 CHECK 와 같은 셋이다
# (`migrations/0021_side_workflows.sql`). 크롤 실행과 낱말을 맞추되 `test` 는 없다
SCHEDULE = "schedule"
AFTER_CRAWL = "after_crawl"
MANUAL = "manual"

# 그 종류가 실제로 하는 일. 카운트를 채우고, 실패했으면 사유 한 줄을 돌려준다.
#
# 돌려준 사유는 `error_message` 로 들어가고 실행은 `failed` 로 닫힌다. 예외를 던져도 되지만,
# 던지지 않고 실패를 말할 길이 있어야 한다 — 분류는 키가 없어도 예외 없이 돌아온다
Body = Callable[[sqlite3.Connection, store.SideWorkflow, SideRunCounts], str | None]


@dataclass(frozen=True)
class Claim:
    """실행 자리 하나를 잡은 결과. 열린 행이거나, 이미 닫힌 건너뜀 행이다."""

    run_id: int
    workflow: store.SideWorkflow
    # 건너뛴 차례면 False 다. 그때 `run_id` 는 이미 `skipped` 로 닫혀 있다
    started: bool


def claim(conn: sqlite3.Connection, side_workflow_id: int, trigger: str) -> Claim:
    """실행 행을 연다. 워크플로우가 없으면 `SideWorkflowNotFoundError` 다.

    설정을 표에서 다시 읽는 자리가 여기다. 부르는 쪽은 id 만 안다.

    앞 실행이 아직 돌고 있으면 열지 않고 건너뛴 행을 남긴다. 조용히 사라지면 주기가 돌지만
    아무것도 못 하는 상태와 주기가 아예 죽은 상태가 같아 보인다 (PRD 2절).

    보는 것과 적는 것을 한 트랜잭션에 넣는다. 주기와 화면이 같은 순간에 들어오면 둘 다 "지금
    도는 것이 없다" 를 읽고 둘 다 열 수 있고, 그 둘은 같은 공고를 두 번 분류한다.
    """
    workflow = store.read(conn, side_workflow_id)
    if workflow is None:
        raise store.SideWorkflowNotFoundError(f"부가 워크플로우가 없다: {side_workflow_id}")
    conn.execute("BEGIN IMMEDIATE")
    try:
        blocked = _blocked(conn, workflow)
        run_id = (
            runs.skipped(conn, workflow.id, trigger, blocked)
            if blocked
            else runs.start(conn, workflow.id, trigger)
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    if blocked:
        logger.info("부가 워크플로우 %s 의 이번 차례를 건너뛴다: %s", workflow.id, blocked)
    return Claim(run_id=run_id, workflow=workflow, started=not blocked)


def _blocked(conn: sqlite3.Connection, workflow: store.SideWorkflow) -> str | None:
    """지금 시작하면 안 되는 이유. 없으면 None 이다.

    두 가지를 본다. 자기 자신이 아직 돌고 있는가와, 같은 일을 다른 경로가 돌고 있는가다.
    뒤엣것이 없으면 화면에서 건 분류와 `POST /api/classify` 로 건 분류가 같은 공고에 두 번
    돈을 쓴다.
    """
    running = runs.open_run(conn, workflow.id)
    if running is not None:
        return f"앞 실행 {running.id} 이 아직 돌고 있다"
    if workflow.kind == store.CLASSIFY and get_classify_run().progress().running:
        return "`POST /api/classify` 로 시작한 분류가 아직 돌고 있다"
    return None


def classify_running(conn: sqlite3.Connection) -> str | None:
    """분류를 도는 부가 워크플로우가 있으면 그 사유. 없으면 None 이다.

    `_blocked` 의 반대 방향이다. 부가 워크플로우 밖에서 분류를 걸려는 경로가 이것을 보고
    물러난다 — 겹침 방지가 한쪽에만 걸리면 막으나 마나다.
    """
    for run in runs.open_runs(conn):
        workflow = store.read(conn, run.side_workflow_id)
        if workflow is not None and workflow.kind == store.CLASSIFY:
            return f"부가 워크플로우 {workflow.id}({workflow.name}) 의 분류가 아직 돌고 있다"
    return None


def run_claimed(
    conn: sqlite3.Connection, workflow: store.SideWorkflow, run_id: int, body: Body
) -> SideRun:
    """열어 둔 행 하나를 채우고 닫는다. 어떤 종료 경로에서도 닫힌다.

    예외는 여기서 끝난다. 사유를 행에 적고 로그에 남기며, 부르는 쪽에는 닫힌 행을 돌려준다.
    `KeyboardInterrupt` 와 종료 신호만은 다시 올린다 — 그것은 이 실행의 실패가 아니라
    프로세스가 내려가는 중이라는 뜻이고, 삼키면 종료가 늦어진다.
    """
    counts = SideRunCounts()
    try:
        reason = body(conn, workflow, counts)
    except Exception as exc:
        logger.exception("부가 워크플로우 %s 실행 %s 가 예외로 끝났다", workflow.id, run_id)
        runs.finish(
            conn, run_id, status=FAILED, counts=counts, error_message=f"{type(exc).__name__}: {exc}"
        )
        return _row(conn, run_id)
    except BaseException as exc:
        runs.finish(
            conn, run_id, status=FAILED, counts=counts, error_message=f"{type(exc).__name__}: {exc}"
        )
        raise
    runs.finish(
        conn,
        run_id,
        status=FAILED if reason else SUCCESS,
        counts=counts,
        error_message=reason,
    )
    return _row(conn, run_id)


def run_once(
    conn: sqlite3.Connection, side_workflow_id: int, body: Body, *, trigger: str = MANUAL
) -> SideRun:
    """실행 한 번을 처음부터 끝까지 돈다. 돌아올 때 행은 닫혀 있다."""
    taken = claim(conn, side_workflow_id, trigger)
    if not taken.started:
        return _row(conn, taken.run_id)
    return run_claimed(conn, taken.workflow, taken.run_id, body)


def _row(conn: sqlite3.Connection, run_id: int) -> SideRun:
    run = runs.read(conn, run_id)
    if run is None:  # pragma: no cover - 방금 적은 행이 사라지는 경로는 없다
        raise LookupError(f"방금 적은 실행을 다시 읽지 못했다: {run_id}")
    return run


def run_now(
    conn: sqlite3.Connection,
    side_workflow_id: int,
    *,
    trigger: str = MANUAL,
    client: Any | None = None,
    settings: Settings | None = None,
) -> SideRun:
    """그 워크플로우를 종류에 맞게 한 번 돈다. 돌아올 때 행은 닫혀 있다.

    `client` 와 `settings` 는 테스트가 가짜 제공자를 넣는 자리다. 비워 두면 화면에서 고른
    제공자와 모델을 실행할 때 읽는다 (`app/llm/settings.py`).
    """
    return run_once(conn, side_workflow_id, _body(client, settings), trigger=trigger)


def _body(client: Any | None, settings: Settings | None) -> Body:
    """종류에 맞는 일. 아직 돌릴 수 없는 종류는 사유를 남기고 실패로 닫힌다."""

    def body(
        conn: sqlite3.Connection, workflow: store.SideWorkflow, counts: SideRunCounts
    ) -> str | None:
        if workflow.kind == store.CLASSIFY:
            return _classify(conn, workflow, counts, client=client, settings=settings)
        # 전달은 설정과 화면까지가 이번 범위이고 아무것도 보내지 않는다 (PRD 3절). 조용히
        # 성공으로 닫으면 보낸 적 없는 실행이 성공 이력으로 쌓인다
        return f"{workflow.kind} 종류는 아직 실행할 수 없다. 이 워크플로우는 아무것도 보내지 않는다"

    return body


def _classify(
    conn: sqlite3.Connection,
    workflow: store.SideWorkflow,
    counts: SideRunCounts,
    *,
    client: Any | None,
    settings: Settings | None,
) -> str | None:
    """대상 범위로 공고를 고르고 1회 상한만큼 잘라 분류에 넘긴다.

    **분류 자체는 여기서 하지 않는다.** 무엇을 돌릴지만 고르고 `app/classify/batch.py` 에
    넘긴다. 실행기가 분류를 다시 구현하면 화면에서 부른 분류와 주기로 도는 분류가 서로 다른
    코드가 되고, 한쪽만 고쳐지는 날이 온다.

    `target_days` 는 그대로 넘긴다. `recent` 가 아닌 범위에서는 값이 NULL 이고, 조회가
    그것을 보지 않는다 (`app/classify/store.py`).
    """
    raw_job_ids = scope_ids(
        conn, workflow.target_scope, days=workflow.target_days, limit=workflow.batch_limit
    )
    counts.target_count = len(raw_job_ids)
    if not raw_job_ids:
        # 대상이 없는 것은 실패가 아니다. 다만 아무 일도 없었다는 사실은 적힌다
        counts.note = f"{workflow.target_scope} 범위에 대상이 없다"
        return None

    progress = ClassifyProgress()
    asyncio.run(
        classify_ids(conn, raw_job_ids, progress, client=client, settings=settings, recheck=True)
    )
    counts.processed_count = progress.processed
    counts.failed_count = progress.failed
    if not progress.failed:
        return None
    reasons = "; ".join(progress.errors)
    if progress.processed:
        # 일부만 실패한 실행은 성공이다. 나머지는 실제로 분류됐고, 실패한 건은 행이 생기지
        # 않아 다음 실행이 다시 집어 든다 (`app/classify/batch.py`)
        counts.note = f"{counts.target_count}건 중 {progress.failed}건 실패: {reasons}"
        return None
    # 한 건도 처리하지 못했다. 제공자 키가 없거나 호출이 전부 실패한 경우이고, 그것을 성공으로
    # 닫으면 운영자가 보는 것은 "돌았는데 대상이 없었다" 와 구분되지 않는다
    return reasons


def start(
    conn: sqlite3.Connection,
    connect: ConnectFactory,
    side_workflow_id: int,
    *,
    trigger: str = MANUAL,
    client: Any | None = None,
    settings: Settings | None = None,
) -> SideRun:
    """실행을 걸고 곧바로 돌아온다. 돌아온 행은 아직 열려 있다.

    자리를 잡는 것(`claim`)은 부르는 쪽 연결에서 그 자리에서 한다. 스레드에 맡기면 응답이
    나간 뒤에 행이 생겨서, 곧바로 진행을 물어본 화면이 "실행한 적 없음" 을 본다.

    일은 자기 연결을 연 스레드가 한다. 요청 연결은 응답과 함께 닫히므로 쓸 수 없다
    (`app/api/rules.py` 의 `get_connect_factory`).

    진행 상황을 메모리에 두지 않는다. `side_runs` 행이 그것을 말하고, 그 행은 프로세스가
    다시 떠도 남는다 — 재정규화와 분류가 메모리에 두어 서버가 뜨면 사라지는 문제를
    되풀이하지 않는다 (PRD 지금 상태).
    """
    taken = claim(conn, side_workflow_id, trigger)
    if not taken.started:
        return _row(conn, taken.run_id)
    threading.Thread(
        target=_work,
        args=(connect, taken, client, settings),
        name=f"side:{side_workflow_id}",
        daemon=True,
    ).start()
    return _row(conn, taken.run_id)


def trigger_after_crawl(conn: sqlite3.Connection, *, new_count: int) -> None:
    """크롤 실행이 끝난 자리에서 부른다. 새 공고가 있을 때만 `after_crawl` 분류를 잇는다.

    `app/crawler/runner.py` 의 실행 끝, `notify_new_jobs` 를 부르는 것과 같은 자리다
    (`app/notify/new_jobs.py`). 그 모듈과 같은 규칙 셋을 따른다.

    **적재 건수가 0 이면 아무것도 하지 않는다.** 신규가 하루 0~1건이라 이 조건이 없으면
    대상 없는 실행이 사이트 수만큼 쌓인다 (PRD 6절).

    **여기서 예외가 나가지 않는다.** 부르는 자리가 크롤 실행의 끝이라, 분류 쪽 사고 하나가
    수집을 실패로 만들면 안 된다. 워크플로우 하나가 걸려도 나머지는 계속 돈다 — 하나가
    실패했다고 전부 건너뛰면 그 이유를 알 길이 없다.

    `import app.db` 를 함수 안에서 한다. 이 모듈이 최상단에서 `app.db` 를 물면 이 모듈을
    임포트하는 모든 자리가 그 무게를 진다 — 실제로 쓰는 곳은 이 함수 하나뿐이다.
    """
    if new_count <= 0:
        return
    from app import db

    for workflow in store.list_all(conn):
        if workflow.kind != store.CLASSIFY:
            continue
        if workflow.trigger_kind != "after_crawl" or workflow.status != store.ACTIVE:
            continue
        try:
            start(conn, db.connect, workflow.id, trigger=AFTER_CRAWL)
        except Exception:
            logger.exception("수집 직후 분류 워크플로우 %s 를 걸지 못했다", workflow.id)


def _work(
    connect: ConnectFactory, taken: Claim, client: Any | None, settings: Settings | None
) -> None:
    """스레드가 도는 자리. 여기서 올라간 예외는 아무도 보지 못한다."""
    try:
        conn = connect()
    except Exception:  # pragma: no cover - 연결을 못 여는 상황은 여기서 만들 수 없다
        # 행을 닫을 연결조차 없다. 열린 채로 남고 다음 기동의 `close_orphans` 가 닫는다
        logger.exception("부가 워크플로우 %s 의 실행 연결을 열지 못했다", taken.workflow.id)
        return
    try:
        run_claimed(conn, taken.workflow, taken.run_id, _body(client, settings))
    finally:
        conn.close()
