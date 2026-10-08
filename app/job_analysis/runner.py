"""공고 분석 한 번 (2026-10-08 결정, LC-3446).

1. 오공고 서버에서 분석할 공고를 `MAX_PER_RUN` 건까지 받는다
2. 공고마다 지금 판의 분석 방법으로 AI 를 부르고, 결과를 서버로 보낸다
3. **이미 같은 본문·같은 판으로 분석해 두고 보내지 못한 공고는 AI 를 다시 부르지 않고 보내기만
   한다**
4. 멈춤 시각이 지나면 새 공고를 부르지 않는다. 남은 공고는 다음 실행이 이어서 한다
5. AI 가 연달아 `MAX_FAILURES_IN_ROW` 번 실패하면 멈춘다 — 제공자가 죽은 날 공고마다 실패를 쌓지
   않는다

실패한 공고는 서버가 다음 실행에도 대상으로 내주므로 따로 다시 부를 목록을 두지 않는다.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from app.config import Settings
from app.job_analysis import guide as guides
from app.job_analysis import ogonggo, store
from app.job_analysis.analyzer import AnalysisError, analyze
from app.job_analysis.posting import Posting

logger = logging.getLogger(__name__)

SCHEDULE = "schedule"
MANUAL = "manual"
# 한 번에 분석하는 공고 수의 상한 (2026-10-08 결정). 처음 적용할 때 쌓인 공고는 며칠에 걸쳐 나눠
# 돈다
MAX_PER_RUN = 200
MAX_FAILURES_IN_ROW = 5


@dataclass
class RunSummary:
    run_id: int = 0
    status: str = "success"
    targets: int = 0
    analyzed: int = 0
    sent: int = 0
    failed: int = 0
    left: int = 0
    notes: list[str] = field(default_factory=list)


async def run_once(
    conn: sqlite3.Connection,
    *,
    trigger: str = MANUAL,
    settings: Settings | None = None,
    stop_at: datetime | None = None,
    llm_client: object | None = None,
    max_per_run: int = MAX_PER_RUN,
) -> RunSummary:
    """분석 한 번. 실행 기록은 `job_analysis_runs` 에 남고, 예외가 나도 기록은 닫는다.

    `stop_at` 은 멈춤 시각이다(시간대가 있는 값). 없으면 대상을 다 돌 때까지 간다.
    """
    summary = RunSummary()
    cursor = conn.execute(
        "INSERT INTO job_analysis_runs (trigger, status) VALUES (?, 'running')", (trigger,)
    )
    summary.run_id = int(cursor.lastrowid or 0)
    try:
        version = guides.current(conn)
        async with ogonggo.connection(conn, settings) as client:
            found = await ogonggo.targets(client, max_per_run)
            summary.targets = len(found)
            await _work(conn, client, found, version, summary, settings, stop_at, llm_client)
    except (ogonggo.OgonggoUnavailable, guides.GuideError) as exc:
        summary.status = "failed"
        summary.notes.append(str(exc))
    except Exception as exc:
        logger.exception("공고 분석이 예외로 끝났다")
        summary.status = "failed"
        summary.notes.append(f"{type(exc).__name__}: {exc}")
    finally:
        conn.execute(
            """
            UPDATE job_analysis_runs
               SET status = ?, target_count = ?, analyzed_count = ?, sent_count = ?,
                   failed_count = ?, left_count = ?, notes = ?, finished_at = datetime('now')
             WHERE id = ?
            """,
            (
                summary.status,
                summary.targets,
                summary.analyzed,
                summary.sent,
                summary.failed,
                summary.left,
                "\n".join(summary.notes)[:4000],
                summary.run_id,
            ),
        )
    logger.info(
        "공고 분석 %s: 대상 %s, 분석 %s, 보냄 %s, 실패 %s, 남음 %s",
        summary.status,
        summary.targets,
        summary.analyzed,
        summary.sent,
        summary.failed,
        summary.left,
    )
    return summary


async def _work(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    found: list[ogonggo.Target],
    version: guides.GuideVersion,
    summary: RunSummary,
    settings: Settings | None,
    stop_at: datetime | None,
    llm_client: object | None,
) -> None:
    failures_in_row = 0
    for index, target in enumerate(found):
        if stop_at is not None and datetime.now(stop_at.tzinfo) >= stop_at:
            summary.left = len(found) - index
            summary.notes.append(
                f"멈춤 시각({stop_at:%H:%M})이 되어 {summary.left}건은 다음 실행에 넘겼다"
            )
            return
        if failures_in_row >= MAX_FAILURES_IN_ROW:
            summary.left = len(found) - index
            summary.status = "failed"
            summary.notes.append(
                f"AI 가 {MAX_FAILURES_IN_ROW}번 연달아 실패해 멈췄다. 설정 > AI 를 확인한다"
            )
            return
        reuse = _reusable(store.get(conn, target.job_id), target, version.number)
        store.save_target(conn, target.job_id, target.source, target.job, target.content_hash)
        if reuse is None:
            try:
                result = await analyze(
                    conn,
                    Posting.of(target.job),
                    version.guide,
                    settings=settings,
                    client=llm_client,
                    guide_version=version.number,
                )
            except AnalysisError as exc:
                store.mark(
                    conn, target.job_id, store.FAILED, str(exc), run_id=summary.run_id, counted=True
                )
                summary.failed += 1
                failures_in_row += 1
                continue
            failures_in_row = 0
            reuse = store.Analyzed(
                result.analysis,
                version.number,
                result.model,
                result.input_tokens,
                result.output_tokens,
            )
            store.save_analysis(conn, target.job_id, reuse, summary.run_id)
            summary.analyzed += 1
        if await send(conn, client, target.job_id, target.content_hash, reuse, summary.run_id):
            summary.sent += 1
        else:
            summary.failed += 1


async def send(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    job_id: int,
    content_hash: str,
    analyzed: store.Analyzed,
    run_id: int | None = None,
) -> bool:
    """분석을 오공고로 보내고 상태를 적는다. 보냈으면 True."""
    result = await ogonggo.put(
        client,
        job_id,
        content_hash=content_hash,
        analysis=analyzed.analysis,
        guide_version=analyzed.guide_version,
        model=analyzed.model,
    )
    if result.outcome == ogonggo.SENT:
        store.mark(conn, job_id, store.SENT, run_id=run_id)
        return True
    status = store.STALE if result.outcome == ogonggo.STALE else store.UNSENT
    store.mark(conn, job_id, status, result.message, run_id=run_id)
    return False


def analyzed_of(row: sqlite3.Row) -> store.Analyzed | None:
    """행에 남은 분석. 다시 보내기가 쓴다."""
    analysis = store.analysis_of(row)
    if analysis is None:
        return None
    return store.Analyzed(
        analysis,
        row["guide_version"],
        str(row["model"]),
        int(row["input_tokens"]),
        int(row["output_tokens"]),
    )


def _reusable(
    row: sqlite3.Row | None, target: ogonggo.Target, version: int
) -> store.Analyzed | None:
    """같은 본문·같은 판으로 분석해 두고 보내지 못한 것이면 그 분석."""
    if row is None or row["status"] != store.UNSENT:
        return None
    if row["content_hash"] != target.content_hash or row["guide_version"] != version:
        return None
    return analyzed_of(row)


def close_orphan_runs(conn: sqlite3.Connection) -> int:
    """지난 프로세스가 끝내지 못한 실행 기록을 실패로 닫는다. 기동할 때 한 번 부른다."""
    try:
        cursor = conn.execute(
            "UPDATE job_analysis_runs SET status = 'failed', finished_at = datetime('now'),"
            " notes = '프로세스가 끝나 분석이 중간에 멈췄다' WHERE status = 'running'"
        )
    except sqlite3.OperationalError:
        return 0
    return cursor.rowcount
