"""미래내일 일경험 CSV 올리기 한 번 (2026-11-16 결정, LC-3605).

포털을 직접 수집하지 않는다 — 운영 서버 IP 가 포털 방화벽에 막혔다 (`source.py`). 사람이 로컬에서
프로그램번호·공유주소를 붙인 공공데이터 CSV 를 화면에서 올린다.

1. **CSV 의 줄을 `work_experiences` 에 넣거나 바꾼다.** 모집이 이미 끝난 줄은 넣지 않는다. 값이
   그대로인 프로그램은 AI 를 다시 부르지 않는다
2. 새 프로그램과 바뀐 프로그램, 앞서 정리에 실패한 프로그램을 AI 로 채운다. 한 번에
   `MAX_FILLS_PER_RUN` 건까지다
3. 전송이 켜져 있으면 오공고로 보낸다. 오공고에 들어간 공고는 공고 분석이 가져가 분석한다
4. **남은 프로그램이 있으면 바로 다음 묶음을 돈다** (`run_all`). 묶음마다 실행 기록이 따로 남는다.
   이번에 실패한 프로그램은 이어지는 묶음에서 다시 부르지 않는다

포털에는 요청을 보내지 않는다.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, field
from typing import Protocol

from app.config import Settings, get_settings
from app.notify import work_report
from app.work_experience import deliver, source, store
from app.work_experience import settings as work_settings
from app.work_experience.fill import LlmProgramFiller, ProgramFill, ProgramFillError

logger = logging.getLogger(__name__)

MANUAL = "manual"
# 한 번에 AI 로 정리하는 프로그램 수의 상한. 남은 프로그램은 이어지는 묶음이 채운다
MAX_FILLS_PER_RUN = 30
# 이어 도는 묶음 수의 상한. 무엇이 꼬여도 한없이 돌지 않게 넉넉히 둔다
MAX_BATCHES = 30

Programs = Sequence[tuple[source.ListItem, source.Program]]


class Filler(Protocol):
    async def fill(self, program: source.Program) -> ProgramFill: ...


@dataclass
class RunSummary:
    run_id: int = 0
    status: str = "success"
    # 올린 CSV 에서 읽은 프로그램 수. 정리만 이어 하는 묶음은 0 이다
    listed: int = 0
    new: int = 0
    # 넣지 않은 줄: 모집이 끝났거나 값이 그대로다
    skipped: int = 0
    fill_failed: int = 0
    filled: int = 0
    sent: int = 0
    failed: int = 0
    # 상한에 닿아 이번에 정리하지 못한 프로그램 수. 0 보다 크면 다음 묶음이 이어서 돈다
    deferred: int = 0
    # 이번에 정리에 실패한 행 id. 이어지는 묶음은 이것을 다시 부르지 않는다
    failed_ids: set[int] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)


async def run_once(
    conn: sqlite3.Connection,
    *,
    programs: Programs = (),
    skipped_lines: Sequence[str] = (),
    filler: Filler | None = None,
    settings: Settings | None = None,
    max_fills: int = MAX_FILLS_PER_RUN,
    exclude: frozenset[int] | set[int] = frozenset(),
) -> RunSummary:
    """묶음 한 번. `programs` 가 있으면 먼저 넣는다. 실행 기록은 예외가 나도 닫는다."""
    resolved = settings or get_settings()
    ai = filler or LlmProgramFiller(conn, settings=settings)
    summary = RunSummary()
    cursor = conn.execute(
        "INSERT INTO work_experience_runs (trigger, status) VALUES (?, 'running')", (MANUAL,)
    )
    summary.run_id = int(cursor.lastrowid or 0)
    try:
        today = deliver.today(resolved)
        _import(conn, programs, today, summary)
        summary.notes.extend(skipped_lines)
        await _fill(conn, ai, today, summary, max_fills, exclude)
        if work_settings.read_config(conn).deliver_enabled:
            delivered = await deliver.deliver_pending(conn, settings=settings)
            summary.sent = delivered.sent
            summary.failed += delivered.failed
            if delivered.reason:
                summary.notes.append(f"오공고로 보내지 않았다: {delivered.reason}")
            summary.notes.extend(delivered.errors)
    except Exception as exc:
        logger.exception("미래내일 일경험 정리가 예외로 끝났다")
        summary.status = "failed"
        summary.notes.append(f"{type(exc).__name__}: {exc}")
    finally:
        conn.execute(
            """
            UPDATE work_experience_runs
               SET status = ?, listed_count = ?, new_count = ?, skipped_count = ?,
                   filled_count = ?, sent_count = ?, failed_count = ?, notes = ?,
                   finished_at = datetime('now')
             WHERE id = ?
            """,
            (
                summary.status,
                summary.listed,
                summary.new,
                summary.skipped,
                summary.filled,
                summary.sent,
                summary.failed,
                "\n".join(summary.notes)[:4000],
                summary.run_id,
            ),
        )
    logger.info(
        "미래내일 일경험 %s: CSV %s, 새 %s, 건너뜀 %s, 채움 %s, 보냄 %s, 실패 %s",
        summary.status,
        summary.listed,
        summary.new,
        summary.skipped,
        summary.filled,
        summary.sent,
        summary.failed,
    )
    return summary


async def run_all(
    conn: sqlite3.Connection,
    *,
    programs: Programs = (),
    skipped_lines: Sequence[str] = (),
    filler: Filler | None = None,
    settings: Settings | None = None,
    max_fills: int = MAX_FILLS_PER_RUN,
    max_batches: int = MAX_BATCHES,
    slot: Callable[[], AbstractAsyncContextManager[object]] | None = None,
) -> list[RunSummary]:
    """CSV 를 넣고, 남은 프로그램이 없어질 때까지 정리 묶음을 이어 돈다.

    멈추는 때: 남은 프로그램이 없다, 실행이 실패했다, 한 건도 정리하지 못했다(AI 가 계속 실패하는
    날), 묶음 상한에 닿았다. `slot` 은 묶음마다 잡았다 놓는다 — AI 호출이 공고 수집과 겹치지 않게
    한다.
    """
    enter = slot or nullcontext
    excluded: set[int] = set()
    summaries: list[RunSummary] = []
    while len(summaries) < max_batches:
        first = not summaries
        async with enter():
            summary = await run_once(
                conn,
                programs=programs if first else (),
                skipped_lines=skipped_lines if first else (),
                filler=filler,
                settings=settings,
                max_fills=max_fills,
                exclude=excluded,
            )
        summaries.append(summary)
        excluded |= summary.failed_ids
        if summary.status != "success" or not summary.deferred or not summary.filled:
            break
    await _report(summaries)
    return summaries


async def _report(summaries: list[RunSummary]) -> None:
    first = summaries[0] if summaries else RunSummary()
    last = summaries[-1] if summaries else RunSummary()
    lines = [
        f"CSV {first.listed}건 · 새 프로그램 {first.new}건 · 건너뜀 {first.skipped}건",
        f"AI 정리 {sum(item.filled for item in summaries)}건: "
        f"실패 {sum(item.fill_failed for item in summaries)}건",
        f"오공고 전송 {sum(item.sent for item in summaries)}건: "
        f"실패 {sum(item.failed - item.fill_failed for item in summaries)}건",
        f"남은 프로그램 {last.deferred}건",
        *last.notes[:20],
    ]
    await work_report.report("미래내일 일경험 CSV 올리기", lines, ok=last.status == "success")


def _import(conn: sqlite3.Connection, programs: Programs, today: str, summary: RunSummary) -> None:
    """CSV 의 프로그램을 넣는다. 모집이 끝난 것은 넣지 않는다 — AI 비용만 들고 보내지도 않는다."""
    summary.listed = len(programs)
    for item, program in programs:
        end = item.recruitment_end
        if end is None or end.isoformat() < today:
            summary.skipped += 1
            continue
        _, state = store.upsert(conn, item, program)
        if state == store.NEW:
            summary.new += 1
        elif state == store.SAME:
            summary.skipped += 1
    if programs:
        store.register_companies(conn)


async def _fill(
    conn: sqlite3.Connection,
    filler: Filler,
    today: str,
    summary: RunSummary,
    max_fills: int,
    exclude: frozenset[int] | set[int],
) -> None:
    for row in store.to_fill(conn, today, exclude):
        row_id = int(row["id"])
        if summary.filled + summary.fill_failed >= max_fills:
            summary.deferred += 1
            continue
        try:
            filled = await filler.fill(store.program_of(row))
        except ProgramFillError as exc:
            _fill_failed(conn, row, str(exc), summary)
            continue
        if not (filled.responsibilities or filled.qualifications):
            _fill_failed(conn, row, "AI 가 주요 업무와 자격 요건을 둘 다 비워 뒀다", summary)
            continue
        store.save_fill(conn, row_id, filled)
        summary.filled += 1
    if summary.deferred:
        summary.notes.append(
            f"한 번에 AI 로 정리하는 상한({max_fills}건)에 닿아"
            f" {summary.deferred}건은 다음 묶음에서 정리한다"
        )


def _fill_failed(
    conn: sqlite3.Connection, row: sqlite3.Row, error: str, summary: RunSummary
) -> None:
    store.save_fill_error(conn, int(row["id"]), error)
    summary.failed += 1
    summary.fill_failed += 1
    summary.failed_ids.add(int(row["id"]))
    summary.notes.append(f"{row['external_id']}: {error}")


def close_orphan_runs(conn: sqlite3.Connection) -> int:
    """지난 프로세스가 끝내지 못한 기록을 실패로 닫는다. 기동할 때 한 번 부른다.

    표가 아직 없는 DB(마이그레이션 전)면 0 이다.
    """
    try:
        cursor = conn.execute(
            "UPDATE work_experience_runs SET status = 'failed', finished_at = datetime('now'),"
            " notes = '프로세스가 끝나 정리가 중간에 멈췄다' WHERE status = 'running'"
        )
    except sqlite3.OperationalError:
        return 0
    return cursor.rowcount
