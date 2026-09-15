"""원문 다시 수집. 이미 담은 공고의 상세를 지금 코드로 다시 가져와 같은 행에 갈아 끼우고, 그
워크플로우의 공고를 전부 다시 분류한다 (2026-09-14 결정).

**왜 따로 있나.** 수집 실행은 이미 아는 주소의 상세를 열지 않는다 (`runner._collect`). 원문을
만드는 방식이 바뀌어도(상세 API 응답 전체, 구조 데이터, 이미지 글) 이미 담은 공고에는 닿지 않고,
다시 분류만 하면 옛 원문(없으면 본문)을 그대로 읽어 빈 칸이 그대로 빈다.

지키는 것.

- **지금 값을 지우지 않는다.** 갈아 끼우기 전 `raw_data_json`·`content_hash`·`crawled_at` 을
  `raw_job_history` 에 그대로 남긴다. 새로 가져온 값이 지금 값과 같으면 갈아 끼우지도 남기지도
  않는다 (`migrations/0030_recollect_source.sql`).
- **공고 번호와 수집 시각은 그대로다.** 사람 보정·제안·전달 표시·나눈 공고가 `raw_jobs.id` 에
  붙어 있고, `crawled_at` 은 대시보드의 일별 추가와 `recent` 범위가 읽는다.
- **저장된 마감일이 지난 공고는 열지 않는다.** 읽지 못한 마감일은 진행 중으로 본다 — 수집 실행과
  같은 판정이다 (`app/crawler/deadline.py`).
- **못 가져오면 지금 값을 둔다.** 상세가 실패하거나 본문이 비면 그 공고는 그대로고, 실패는 실행
  기록에 남는다.
- **실행 기록은 `crawl_runs.trigger = 'recollect'` 다.** 자동 중지의 연속 실패에는 세지 않는다
  (`runner.consecutive_failures`).
- **다시 분류는 그 워크플로우의 공고 전부다.** 원문이 바뀌지 않은 공고와 마감이 지나 다시 못
  가져온 공고도 지금 분류기로 다시 나눈다. 다른 경로가 분류를 돌리는 중이면 끝날 때까지
  기다린다 — 같은 공고에 두 번 돈을 쓰지 않는다.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from typing import Any

from app.classify.batch import (
    ClassifyProgress,
    ClassifyRunningError,
    classify_ids,
    get_classify_run,
)
from app.classify.store import workflow_ids
from app.crawler.collect import Collectors, open_collectors
from app.crawler.deadline import is_closed
from app.crawler.failures import DetailEmptyError, DetailUnreachableError, Failure, classify
from app.crawler.fetcher import FetchPolicy, get_fetcher
from app.crawler.hashing import content_hash
from app.crawler.images import ImageReader, LlmImageReader
from app.crawler.parser import ListItem
from app.crawler.runner import (
    RECOLLECT,
    ItemFailure,
    RunResult,
    RunTarget,
    _config_failure,
    _finish_run,
    _load_rules,
    _record,
    _start_run,
    collect_selectors,
)
from app.normalize.backfill import rewrite_one
from app.normalize.engine import NormalizeError
from app.normalize.rules import Rule
from app.selector.api_schema import ApiConfigError, parse_api_config
from app.selector.schema import SPLIT_DETAIL_FIELDS, SelectorSchemaError
from app.side.runner import classify_running

logger = logging.getLogger(__name__)

# 다른 경로의 분류가 도는 중이면 이 간격으로 다시 본다
WAIT_SECONDS = 15.0

# 다시 분류 결과를 실행 기록에 남기는 메모의 머리말. 카드가 이것으로 찾아 적는다
CLASSIFY_NOTE = "다시 분류"

# 상세가 비운 칸을 채울 때 지금 저장된 값을 쓰는 칸. 목록 응답이 상세 칸의 값을 들고 오는 사이트가
# 있어서(`runner._record`), 목록이 더는 그 값을 주지 않는다고 다시 수집이 비워 버리면 안 된다
_CARRIED: tuple[str, ...] = (
    "body",
    "qualifications",
    "recruitment_end_at",
    "department",
    *SPLIT_DETAIL_FIELDS,
)

Reclassify = Callable[[sqlite3.Connection, list[int], ClassifyProgress], Awaitable[Any]]
Slot = Callable[[], AbstractAsyncContextManager[Any]]


@dataclass
class RecollectProgress:
    """화면이 읽는 진행 상황. 이 프로세스 안에서만 산다."""

    stage: str = "수집"
    targets: int = 0
    done: int = 0
    changed: int = 0
    unchanged: int = 0
    failed: int = 0
    classify: ClassifyProgress | None = None

    def line(self) -> str:
        """카드에 적는 한 줄."""
        if self.stage == "수집":
            return (
                f"원문을 다시 수집하는 중이다 — {self.done}/{self.targets}건 "
                f"(바뀜 {self.changed}, 그대로 {self.unchanged}, 실패 {self.failed})"
            )
        progress = self.classify
        if self.stage == "분류 대기" or progress is None:
            return "원문 다시 수집이 끝났다. 다른 분류가 끝나기를 기다렸다가 다시 분류한다"
        return (
            f"다시 분류하는 중이다 — {progress.processed + progress.failed}/{progress.total}건 "
            f"(실패 {progress.failed}, 토큰 {progress.total_tokens:,}개)"
        )


# 워크플로우 id → 지금 도는 원문 다시 수집. 카드가 진행 상황을 읽는다
PROGRESS: dict[int, RecollectProgress] = {}


@dataclass(frozen=True)
class RecollectPreview:
    """시작하기 전 확인에 적는 건수."""

    # 워크플로우의 공고. 같은 주소가 여러 행이면 하나로 센다
    total: int
    # 저장된 마감일이 안 지나 상세를 다시 여는 공고
    targets: int
    closed: int
    # 다시 분류하는 공고. 보낼 글이 있는 행 전부다
    classify: int


@dataclass(frozen=True)
class _Stored:
    raw_job_id: int
    source_url: str
    record: dict[str, Any]


def preview(conn: sqlite3.Connection, workflow_id: int) -> RecollectPreview:
    """시작하면 몇 건을 다시 가져오고 몇 건을 다시 분류하는가. 읽기 전용이다."""
    rules, _ = _load_rules(conn)
    stored = _latest(conn, workflow_id)
    closed = sum(1 for row in stored if _closed(row, rules))
    return RecollectPreview(
        total=len(stored),
        targets=len(stored) - closed,
        closed=closed,
        classify=len(workflow_ids(conn, workflow_id)),
    )


async def recollect_workflow(
    conn: sqlite3.Connection,
    workflow_id: int,
    *,
    fetcher: FetchPolicy | None = None,
    collectors: Collectors | None = None,
    image_reader: ImageReader | None = None,
    slot: Slot | None = None,
    reclassify: Reclassify | None = None,
    wait_seconds: float = WAIT_SECONDS,
) -> RunResult:
    """워크플로우 하나의 원문을 다시 수집하고 공고를 전부 다시 분류한다.

    `slot` 은 페이지를 가져오는 동안만 잡는 동시 실행 자리다. 다시 분류는 사이트에 요청을 보내지
    않는데 한 시간 넘게 자리를 붙들면 다른 워크플로우의 수집이 밀린다.

    실행 기록은 다시 분류까지 끝나야 닫힌다. 그동안 카드는 진행 중으로 남는다.

    `collectors` 와 `reclassify` 는 시험이 갈아 끼우는 자리다.
    """
    row = conn.execute(
        """
        SELECT c.list_url AS list_url, c.selectors_json AS selectors_json,
               c.list_mode AS list_mode, c.detail_mode AS detail_mode,
               c.api_config_json AS api_config_json
          FROM workflows w
          JOIN crawlers c ON c.id = w.crawler_id
         WHERE w.id = ?
        """,
        (workflow_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"워크플로우 {workflow_id} 가 없다")

    list_mode, detail_mode = str(row["list_mode"]), str(row["detail_mode"])
    try:
        selectors = collect_selectors(row["selectors_json"], list_mode, detail_mode)
    except (json.JSONDecodeError, SelectorSchemaError) as exc:
        return _config_failure(conn, workflow_id, RECOLLECT, f"셀렉터를 읽을 수 없다: {exc}")
    try:
        api_config = parse_api_config(row["api_config_json"])
    except ApiConfigError as exc:
        return _config_failure(conn, workflow_id, RECOLLECT, f"API 설정을 읽을 수 없다: {exc}")

    progress = RecollectProgress()
    PROGRESS[workflow_id] = progress
    target = RunTarget(
        list_url=row["list_url"], selectors=selectors, trigger=RECOLLECT, workflow_id=workflow_id
    )
    result = RunResult(run_id=_start_run(conn, target), status="")
    try:
        failure: Failure | None = None
        guard = slot() if slot is not None else contextlib.nullcontext()
        try:
            async with guard:
                if collectors is not None:
                    await _collect_again(conn, workflow_id, collectors, result, progress)
                else:
                    async with open_collectors(
                        list_mode=list_mode,
                        detail_mode=detail_mode,
                        list_url=row["list_url"],
                        selectors=selectors,
                        fetcher=fetcher or get_fetcher(),
                        api_config=api_config,
                        # 목록을 끝까지 넘긴다. 아는 공고에서 멈추면 다시 열 공고의 id 를 못 얻는다
                        known=None,
                        image_reader=image_reader or LlmImageReader(conn),
                    ) as opened:
                        await _collect_again(conn, workflow_id, opened, result, progress)
        except Exception as exc:
            failure = classify(exc)

        progress.stage = "분류 대기"
        await _classify_again(
            conn, workflow_id, result, progress, reclassify or classify_ids, wait_seconds
        )
        _finish_run(conn, result, failure)
    except BaseException as exc:
        # 취소·강제 종료도 종료 경로다. 행을 남기고 나서 그대로 올려보낸다
        if not result.status:
            _finish_run(conn, result, classify(exc))
        raise
    finally:
        PROGRESS.pop(workflow_id, None)
    return result


async def _collect_again(
    conn: sqlite3.Connection,
    workflow_id: int,
    collectors: Collectors,
    result: RunResult,
    progress: RecollectProgress,
) -> None:
    rules, rules_error = _load_rules(conn)
    stored = _latest(conn, workflow_id)
    listed = await _listed(collectors, result)
    targets = [row for row in stored if not _closed(row, rules)]
    result.skipped_count = len(stored) - len(targets)
    progress.targets = len(targets)

    for row in targets:
        found = listed.get(row.source_url)
        item = _item(row, found)
        try:
            record, notes = await _fetch(collectors, item, found)
        except Exception as exc:
            # 공고 하나가 실패해도 나머지는 계속 간다. 그 공고는 지금 값을 둔다
            failed = classify(exc)
            result.fail_count += 1
            result.failures.append(
                ItemFailure(
                    source_url=row.source_url,
                    error_class=failed.error_class,
                    message=failed.message,
                    title=item.title,
                )
            )
            progress.failed += 1
            progress.done += 1
            continue

        result.success_count += 1
        for note in notes:
            result.failures.append(
                ItemFailure(
                    source_url=row.source_url, error_class=None, message=note, title=item.title
                )
            )
        if record == row.record:
            progress.unchanged += 1
        else:
            _replace(conn, row, record, result.run_id)
            progress.changed += 1
            _renormalize(conn, row, rules, rules_error, result)
        progress.done += 1


async def _listed(collectors: Collectors, result: RunResult) -> dict[str, ListItem]:
    """지금 목록에 걸린 항목. 상세 API 에 넘길 id 와 목록이 들고 오는 칸이 여기 있다.

    못 읽어도 멈추지 않는다. 저장된 주소로 상세를 연다 — HTML 상세는 주소만 있으면 되고, 목록 첫
    쪽에서 밀려난 공고도 그렇게 닿는다.
    """
    try:
        parsed = await collectors.list.collect()
    except Exception as exc:
        failed = classify(exc)
        result.failures.append(
            ItemFailure(
                source_url="",
                error_class=None,
                message=f"목록을 읽지 못해 저장된 주소로 상세를 연다: {failed.message}",
            )
        )
        return {}
    return {item.link: item for item in parsed.items}


def _item(row: _Stored, listed: ListItem | None) -> ListItem:
    """상세를 열 항목. 목록에 있으면 목록 항목이고, 없으면 저장된 값으로 만든다.

    상세가 비운 칸을 채울 값(`extra`)에는 목록 값을 먼저, 목록이 주지 않으면 지금 저장된 값을 둔다.
    """
    kept = {name: str(row.record.get(name) or "") for name in _CARRIED}
    if listed is not None:
        given = {name: value for name, value in listed.extra.items() if value}
        return replace(listed, extra={**kept, **given})
    return ListItem(
        index=0,
        title=str(row.record.get("list_title") or row.record.get("title") or ""),
        link=row.source_url,
        date=str(row.record.get("list_date") or ""),
        company_name=str(row.record.get("company_name") or ""),
        extra=kept,
    )


async def _fetch(
    collectors: Collectors, item: ListItem, listed: ListItem | None
) -> tuple[dict[str, str], tuple[str, ...]]:
    """상세를 가져와 `raw_data_json` 에 넣을 값을 만든다. 수집 실행의 `_record` 와 같은 모양이다.

    본문이 비었는지는 지금 저장된 본문을 빼고 본다. 그것까지 보면 본문 셀렉터가 깨진 사이트가
    옛 본문으로 성공처럼 보인다.
    """
    if item.detail_absent:
        raise DetailUnreachableError("상세로 갈 길이 없는 사이트라 원문을 다시 가져올 수 없다")
    detail = await collectors.detail.collect(item)
    fresh_body = detail.fields.get("body", "") or (listed.extra.get("body", "") if listed else "")
    if not fresh_body.strip():
        raise DetailEmptyError("상세를 열었지만 본문이 비었다. 지금 저장된 값을 둔다")
    return _record(item, detail.fields, detail.source_text, detail.cover_image), tuple(detail.notes)


def _replace(conn: sqlite3.Connection, row: _Stored, record: dict[str, str], run_id: int) -> None:
    """지금 값을 이력에 남기고 같은 행에 새 값을 넣는다.

    둘은 한 트랜잭션이다 — 남기지 못했는데 갈아 끼워진 행이 생기면 지금 값이 사라진다.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO raw_job_history
                   (raw_job_id, run_id, raw_data_json, content_hash, crawled_at)
            SELECT id, ?, raw_data_json, content_hash, crawled_at FROM raw_jobs WHERE id = ?
            """,
            (run_id, row.raw_job_id),
        )
        conn.execute(
            "UPDATE raw_jobs SET raw_data_json = ?, content_hash = ? WHERE id = ?",
            (json.dumps(record, ensure_ascii=False), content_hash(record), row.raw_job_id),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _renormalize(
    conn: sqlite3.Connection,
    row: _Stored,
    rules: list[Rule],
    rules_error: str | None,
    result: RunResult,
) -> None:
    """갈아 끼운 공고의 정규화 행을 새 원문에 맞춘다.

    다시 분류가 실패해도 규칙이 만드는 칸(제목·회사·마감일)은 새 원문을 따른다.
    """
    message = rules_error
    if message is None:
        try:
            rewrite_one(conn, row.raw_job_id, rules)
            return
        except NormalizeError as exc:
            message = str(exc)
    result.failures.append(
        ItemFailure(
            source_url=row.source_url,
            error_class=None,
            message=f"원문은 갈아 끼웠으나 정규화하지 못했다: {message}",
        )
    )


async def _classify_again(
    conn: sqlite3.Connection,
    workflow_id: int,
    result: RunResult,
    progress: RecollectProgress,
    reclassify: Reclassify,
    wait_seconds: float,
) -> None:
    """워크플로우의 공고를 전부 다시 분류한다. 다른 분류가 돌면 끝날 때까지 기다린다.

    분류 자리를 잡는 동안 `POST /api/classify` 와 부가 워크플로우의 분류가 물러난다
    (`app/classify/batch.py` 의 `ClassifyRun.claim`). 결과는 실행 기록에 메모 한 줄로 남는다.
    """
    run = get_classify_run()
    while True:
        if classify_running(conn) is None:
            try:
                claimed = run.claim()
                break
            except ClassifyRunningError:
                pass
        await asyncio.sleep(wait_seconds)

    progress.stage = "분류"
    progress.classify = claimed
    ids = workflow_ids(conn, workflow_id)
    try:
        await reclassify(conn, ids, claimed)
    except Exception as exc:
        logger.exception("workflow %s: 원문 다시 수집 뒤 다시 분류가 중단됐다", workflow_id)
        claimed.note(f"다시 분류가 중단됐다: {exc}")
    finally:
        run.release()

    reasons = f" — {'; '.join(claimed.errors[:3])}" if claimed.errors else ""
    result.failures.append(
        ItemFailure(
            source_url="",
            error_class=None,
            message=(
                f"{CLASSIFY_NOTE}: {len(ids)}건 중 처리 {claimed.processed}건, "
                f"실패 {claimed.failed}건, 토큰 {claimed.total_tokens:,}개{reasons}"
            ),
        )
    )


def _latest(conn: sqlite3.Connection, workflow_id: int) -> list[_Stored]:
    """워크플로우의 공고. 같은 주소가 여러 행이면 가장 최근 행 하나다. 최근 수집한 것부터다."""
    rows = conn.execute(
        """
        SELECT id, source_url, raw_data_json
          FROM raw_jobs
         WHERE id IN (SELECT max(id) FROM raw_jobs WHERE workflow_id = ? GROUP BY source_url)
         ORDER BY id DESC
        """,
        (workflow_id,),
    ).fetchall()
    stored: list[_Stored] = []
    for row in rows:
        try:
            record = json.loads(row["raw_data_json"])
        except json.JSONDecodeError:
            record = {}
        stored.append(
            _Stored(
                raw_job_id=int(row["id"]),
                source_url=str(row["source_url"]),
                record=record if isinstance(record, dict) else {},
            )
        )
    return stored


def _closed(row: _Stored, rules: list[Rule]) -> bool:
    return is_closed(str(row.record.get("recruitment_end_at") or ""), rules)
