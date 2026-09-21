"""새싹 부트캠프 수집 한 번 (2026-09-22 결정, LC-3364).

1. 모집 중 목록을 쪽마다 읽는다
2. 과정마다 상세를 받아 파서로 읽고 `bootcamps` 에 넣는다. 읽은 값이 그대로면 손대지 않는다
3. 새로 들어왔거나 바뀐 과정, 앞서 채우기에 실패한 과정을 AI 로 채운다
4. 전송이 켜져 있으면 오공고로 보낸다

요청은 전부 공용 fetch 클라이언트로 나간다 — robots·딜레이·User-Agent 가 공고 수집과 같다.

**목록이 비면 실패다.** `총 N건` 이 0 이 아닌데 카드를 하나도 못 읽었으면 새싹 틀이 바뀐 것이다.
0건 성공으로 적으면 틀이 바뀐 날과 모집 과정이 없는 날이 똑같이 보인다.
"""

from __future__ import annotations

import logging
import sqlite3
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


class Filler(Protocol):
    async def fill(self, course: sesac.Course) -> BootcampFill: ...


@dataclass
class RunSummary:
    run_id: int = 0
    status: str = "success"
    listed: int = 0
    new: int = 0
    changed: int = 0
    filled: int = 0
    sent: int = 0
    failed: int = 0
    notes: list[str] = field(default_factory=list)


async def run_sesac(
    conn: sqlite3.Connection,
    *,
    trigger: str = MANUAL,
    fetcher: FetchPolicy | None = None,
    filler: Filler | None = None,
    settings: Settings | None = None,
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
        await _collect(conn, source, ai, summary)
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
               SET status = ?, listed_count = ?, new_count = ?, changed_count = ?,
                   filled_count = ?, sent_count = ?, failed_count = ?, notes = ?,
                   finished_at = datetime('now')
             WHERE id = ?
            """,
            (
                summary.status,
                summary.listed,
                summary.new,
                summary.changed,
                summary.filled,
                summary.sent,
                summary.failed,
                "\n".join(summary.notes)[:4000],
                summary.run_id,
            ),
        )
    logger.info(
        "부트캠프 수집 %s: 목록 %s, 새 %s, 바뀜 %s, 채움 %s, 보냄 %s, 실패 %s",
        summary.status,
        summary.listed,
        summary.new,
        summary.changed,
        summary.filled,
        summary.sent,
        summary.failed,
    )
    return summary


class _ListBroken(RuntimeError):
    """목록을 읽을 수 없다. 이번 실행은 실패다."""


async def _collect(
    conn: sqlite3.Connection, fetcher: FetchPolicy, filler: Filler, summary: RunSummary
) -> None:
    items = await _list_all(fetcher)
    summary.listed = len(items)
    for item in items:
        try:
            page = await fetcher.fetch(sesac.detail_url(item.crs_sn))
            course = sesac.parse_detail(page.text, item.crs_sn)
        except (FetchError, sesac.SesacParseError) as exc:
            summary.failed += 1
            summary.notes.append(f"{sesac.detail_url(item.crs_sn)}: {exc}")
            continue
        bootcamp_id, state = store.upsert(conn, course, item.thumbnail_url)
        if state == store.NEW:
            summary.new += 1
        elif state == store.CHANGED:
            summary.changed += 1
        if not store.needs_fill(conn, bootcamp_id):
            continue
        try:
            filled = await filler.fill(course)
        except BootcampFillError as exc:
            store.save_fill_error(conn, bootcamp_id, str(exc))
            summary.failed += 1
            summary.notes.append(f"{course.source_url}: {exc}")
            continue
        if not filled.content or not filled.short_description:
            store.save_fill_error(conn, bootcamp_id, "AI 가 상세 내용이나 한 줄 소개를 비워 뒀다")
            summary.failed += 1
            continue
        store.save_fill(conn, bootcamp_id, filled)
        summary.filled += 1


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
