"""새싹 부트캠프 수집 한 번 (2026-09-22 결정, LC-3364).

1. 오프라인 과정 목록을 모집 상태와 상관없이 쪽마다 읽는다
2. **이미 모아 정리까지 끝난 과정은 건너뛴다** (2026-09-22 결정). 상세를 다시 받지도, AI 를 다시
   부르지도, 오공고로 다시 보내지도 않는다. 공고 수집이 아는 주소를 다시 열지 않는 것과 같다
   목록 카드의 모집 상태만 적는다. 바뀌었으면 오공고에 다시 보낸다
3. 새 과정과 앞서 정리에 실패한 과정만 상세를 받아 `bootcamps` 에 넣고 AI 로 채운다. 신청할 수 있는
   과정부터, 한 번에 `MAX_FILLS_PER_RUN` 건까지다
4. 전송이 켜져 있으면 오공고로 보낸다
5. **남은 과정이 있으면 바로 다음 묶음을 돈다** (2026-09-22 결정, `run_sesac_all`). 화면에서 수집을
   여러 번 누르지 않아도 다 정리될 때까지 20건씩 이어진다. 묶음마다 실행 기록이 따로 남는다.
   이번에 실패한 과정은 이어지는 묶음에서 다시 부르지 않고 다음 수집에 맡긴다

요청은 전부 공용 fetch 클라이언트로 나간다 — robots·딜레이·User-Agent 가 공고 수집과 같다.

**목록이 비면 실패다.** `총 N건` 이 0 이 아닌데 카드를 하나도 못 읽었으면 새싹 틀이 바뀐 것이다.
0건 성공으로 적으면 틀이 바뀐 날과 모집 과정이 없는 날이 똑같이 보인다.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, field
from typing import Protocol

from app.bootcamp import deliver, sesac, store
from app.bootcamp import settings as bootcamp_settings
from app.bootcamp.fill import BootcampFill, BootcampFillError, LlmBootcampFiller
from app.config import Settings
from app.crawler.fetcher import FetchError, FetchPolicy, get_fetcher

logger = logging.getLogger(__name__)

SCHEDULE = "schedule"
MANUAL = "manual"
# 한 번의 수집에서 AI 로 정리하는 과정 수의 상한. 처음 수집은 190건 가까이 되는데, 한 번에 다 부르면
# 실행 하나가 한 시간 넘게 돌고 제공자 한도에 걸린다. 남은 과정은 다음 수집이 이어서 채운다
MAX_FILLS_PER_RUN = 20
# 이어 도는 묶음 수의 상한. 새싹 전체가 200건 남짓이라 20건씩 열 번이면 끝난다. 무엇이 꼬여도
# 한없이 돌지 않게 넉넉히 둔다
MAX_BATCHES = 20


class Filler(Protocol):
    async def fill(self, course: sesac.Course) -> BootcampFill: ...


@dataclass
class RunSummary:
    run_id: int = 0
    status: str = "success"
    listed: int = 0
    new: int = 0
    skipped: int = 0
    status_changed: int = 0
    fill_failed: int = 0
    filled: int = 0
    sent: int = 0
    failed: int = 0
    # 상한에 닿아 이번에 정리하지 못한 과정 수. 0 보다 크면 다음 묶음이 이어서 돈다
    deferred: int = 0
    # 이번에 상세를 받지 못했거나 정리에 실패한 과정. 이어지는 묶음은 이것을 다시 부르지 않는다
    failed_urls: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)


async def run_sesac(
    conn: sqlite3.Connection,
    *,
    trigger: str = MANUAL,
    fetcher: FetchPolicy | None = None,
    filler: Filler | None = None,
    settings: Settings | None = None,
    max_fills: int = MAX_FILLS_PER_RUN,
    exclude: frozenset[str] | set[str] = frozenset(),
) -> RunSummary:
    """수집 한 번. 실행 기록은 `bootcamp_runs` 에 남고, 예외가 나도 기록은 닫는다."""
    source = fetcher or get_fetcher()
    ai = filler or LlmBootcampFiller(conn, source, settings=settings)
    summary = RunSummary()
    cursor = conn.execute(
        "INSERT INTO bootcamp_runs (trigger, status) VALUES (?, 'running')", (trigger,)
    )
    summary.run_id = int(cursor.lastrowid or 0)
    try:
        await _collect(conn, source, ai, summary, max_fills, exclude)
        if bootcamp_settings.read_config(conn).deliver_enabled:
            delivered = await deliver.deliver_pending(conn, settings=settings)
            summary.sent = delivered.sent
            summary.failed += delivered.failed
            if delivered.reason:
                summary.notes.append(f"오공고로 보내지 않았다: {delivered.reason}")
            summary.notes.extend(delivered.errors)
    except _ListBroken as exc:
        summary.status = "failed"
        summary.notes.append(str(exc))
    except Exception as exc:
        logger.exception("부트캠프 수집이 예외로 끝났다")
        summary.status = "failed"
        summary.notes.append(f"{type(exc).__name__}: {exc}")
    finally:
        conn.execute(
            """
            UPDATE bootcamp_runs
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
        "부트캠프 수집 %s: 목록 %s, 새 %s, 건너뜀 %s, 채움 %s, 보냄 %s, 실패 %s",
        summary.status,
        summary.listed,
        summary.new,
        summary.skipped,
        summary.filled,
        summary.sent,
        summary.failed,
    )
    return summary


async def run_sesac_all(
    conn: sqlite3.Connection,
    *,
    trigger: str = MANUAL,
    fetcher: FetchPolicy | None = None,
    filler: Filler | None = None,
    settings: Settings | None = None,
    max_fills: int = MAX_FILLS_PER_RUN,
    max_batches: int = MAX_BATCHES,
    slot: Callable[[], AbstractAsyncContextManager[object]] | None = None,
) -> list[RunSummary]:
    """남은 과정이 없어질 때까지 `run_sesac` 를 묶음으로 이어 돈다.

    멈추는 때: 남은 과정이 없다, 실행이 실패했다, 한 건도 정리하지 못했다(AI 가 계속 실패하는 날),
    묶음 상한에 닿았다. `slot` 은 묶음마다 잡았다 놓는다 — 한 시간 가까이 도는 동안 공고 수집이
    끼어들 수 있게 한다.
    """
    enter = slot or nullcontext
    excluded: set[str] = set()
    summaries: list[RunSummary] = []
    while len(summaries) < max_batches:
        async with enter():
            summary = await run_sesac(
                conn,
                trigger=trigger,
                fetcher=fetcher,
                filler=filler,
                settings=settings,
                max_fills=max_fills,
                exclude=excluded,
            )
        summaries.append(summary)
        excluded |= summary.failed_urls
        if summary.status != "success" or not summary.deferred or not summary.filled:
            break
    return summaries


class _ListBroken(RuntimeError):
    """목록을 읽을 수 없다. 이번 실행은 실패다."""


async def _collect(
    conn: sqlite3.Connection,
    fetcher: FetchPolicy,
    filler: Filler,
    summary: RunSummary,
    max_fills: int,
    exclude: frozenset[str] | set[str],
) -> None:
    items = await _list_all(fetcher)
    summary.listed = len(items)
    # 신청할 수 있는 과정부터 채운다. 한 번에 채우는 수에 상한이 있어 먼저 온 것이 먼저
    # 오공고에 간다
    items.sort(key=lambda item: sesac.spring_status(item.status) != "RECRUITING")
    for item in items:
        source_url = sesac.detail_url(item.crs_sn)
        if store.is_done(conn, source_url):
            if store.touch(conn, source_url, item.status):
                summary.status_changed += 1
            summary.skipped += 1
            continue
        if source_url in exclude:
            continue
        if summary.filled + summary.fill_failed >= max_fills:
            summary.deferred += 1
            continue
        try:
            page = await fetcher.fetch(source_url)
            course = sesac.parse_detail(page.text, item.crs_sn)
        except (FetchError, sesac.SesacParseError) as exc:
            summary.failed += 1
            summary.failed_urls.add(source_url)
            summary.notes.append(f"{source_url}: {exc}")
            continue
        bootcamp_id, state = store.upsert(conn, course, item.thumbnail_url, item.status)
        if state == store.NEW:
            summary.new += 1
        if not store.needs_fill(conn, bootcamp_id):
            continue
        try:
            filled = await filler.fill(course)
        except BootcampFillError as exc:
            store.save_fill_error(conn, bootcamp_id, str(exc))
            summary.failed += 1
            summary.fill_failed += 1
            summary.failed_urls.add(source_url)
            summary.notes.append(f"{course.source_url}: {exc}")
            continue
        if not filled.content or not filled.short_description:
            store.save_fill_error(conn, bootcamp_id, "AI 가 상세 내용이나 한 줄 소개를 비워 뒀다")
            summary.failed += 1
            summary.fill_failed += 1
            summary.failed_urls.add(source_url)
            continue
        store.save_fill(conn, bootcamp_id, filled)
        summary.filled += 1
    if summary.deferred:
        summary.notes.append(
            f"한 번에 AI 로 정리하는 상한({max_fills}건)에 닿아"
            f" {summary.deferred}건은 다음 묶음에서 모은다"
        )
    if summary.status_changed:
        summary.notes.append(f"모집 상태가 바뀐 과정 {summary.status_changed}건")


async def _list_all(fetcher: FetchPolicy) -> list[sesac.ListItem]:
    found: dict[str, sesac.ListItem] = {}
    for page_number in range(1, sesac.MAX_PAGES + 1):
        try:
            page = await fetcher.fetch(sesac.list_url(page_number))
        except FetchError as exc:
            raise _ListBroken(f"새싹 목록을 받지 못했다: {exc}") from exc
        items = sesac.parse_list(page.text)
        if page_number == 1 and not items:
            total = sesac.total_count(page.text)
            if total != 0:
                raise _ListBroken(
                    f"새싹 목록에서 과정을 하나도 읽지 못했다(총 {total}건). 틀이 바뀌었는지 본다"
                )
        fresh = [item for item in items if item.crs_sn not in found]
        if not fresh:
            break
        found.update((item.crs_sn, item) for item in fresh)
    return list(found.values())


def close_orphan_runs(conn: sqlite3.Connection) -> int:
    """지난 프로세스가 끝내지 못한 수집 기록을 실패로 닫는다. 기동할 때 한 번 부른다.

    표가 아직 없는 DB(마이그레이션 전)면 0 이다.
    """
    try:
        cursor = conn.execute(
            "UPDATE bootcamp_runs SET status = 'failed', finished_at = datetime('now'),"
            " notes = '프로세스가 끝나 수집이 중간에 멈췄다' WHERE status = 'running'"
        )
    except sqlite3.OperationalError:
        return 0
    return cursor.rowcount
