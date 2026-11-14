"""상시 채용 공고의 원문을 한 바퀴 열어 보고, 내려간 공고를 오공고에서 바로 마감한다 (LC-3621).

대상은 오공고 서버가 준다(`app/source_check/ogonggo.py`). 등록 경로를 가리지 않아 고용24·기업회원
공고도 본다. 판정은 `app/source_check/judge.py` 이고 **마감으로 나온 것만** 마감한다.

## 한 바퀴

- 식별자 순으로 100건씩 받아 본다. 멈춤 시각이 되면 거기서 멈추고, 다 본 쪽의 마지막 식별자를
  `app_settings` 에 남겨 다음 날 이어서 본다. 끝까지 보면 0 으로 되돌린다.
- 같은 원문(직무마다 나뉜 `원문#번호`)은 한 번만 연다.
- 공용 fetch 클라이언트를 쓰므로 robots·호스트별 딜레이를 그대로 지킨다. 호스트가 다른 공고는
  `CONCURRENCY` 개까지 함께 연다.

끝나면 슬랙으로 한 통 보낸다(`app/notify/work_report.py`).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from app.config import Settings, get_settings
from app.crawler.fetcher import FetchError, FetchResult, PageSource, get_fetcher
from app.crawler.playwright import Renderer, RenderUnavailableError
from app.job_analysis.ogonggo import OgonggoUnavailable, connection
from app.notify import work_report
from app.source_check import ogonggo
from app.source_check.judge import CLOSED, OPEN, UNKNOWN, Verdict, fetch_url, judge
from app.source_check.ogonggo import Target

logger = logging.getLogger(__name__)

REPORT_NAME = "상시 채용 원문 확인"
CURSOR_KEY = "source_check_after_id"
PAGE_SIZE = 100
CONCURRENCY = 4
# 슬랙에 마감한 공고를 몇 건까지 적는가
REPORT_CLOSED_LINES = 15


@dataclass
class Checked:
    target: Target
    verdict: Verdict
    close_error: str = ""


@dataclass
class RunResult:
    reason: str = ""
    checked: list[Checked] = field(default_factory=list)
    closed: int = 0
    finished: bool = False

    def count(self, status: str) -> int:
        return sum(item.verdict.status == status for item in self.checked)


class _Renderer:
    """브라우저를 처음 쓸 때 띄우고, 띄우지 못하면 이번 실행 동안 다시 시도하지 않는다."""

    def __init__(self) -> None:
        self._inner: Renderer | None = None
        self._unavailable = False

    async def fetch(self, url: str) -> FetchResult:
        if self._unavailable:
            raise FetchError("브라우저를 쓸 수 없다")
        if self._inner is None:
            self._inner = Renderer(get_fetcher())
        try:
            return await self._inner.fetch(url)
        except RenderUnavailableError:
            self._unavailable = True
            raise

    async def aclose(self) -> None:
        if self._inner is not None:
            await self._inner.aclose()


def read_cursor(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (CURSOR_KEY,)).fetchone()
    try:
        return max(int(row[0]), 0) if row else 0
    except ValueError:
        return 0


def write_cursor(conn: sqlite3.Connection, after_id: int) -> None:
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')",
        (CURSOR_KEY, str(after_id)),
    )
    conn.commit()


async def run_once(
    conn: sqlite3.Connection,
    *,
    stop_at: datetime | None = None,
    close: bool = True,
    limit: int | None = None,
    settings: Settings | None = None,
    fetcher: PageSource | None = None,
    renderer: PageSource | None = None,
    report: bool = True,
) -> RunResult:
    """한 바퀴 돈다. `close` 가 꺼져 있거나 `limit` 이 있으면 손으로 돌려 보는 것이라 이어 볼
    자리를 옮기지 않고 슬랙에도 보내지 않는다."""
    resolved = settings or get_settings()
    manual = not close or limit is not None
    result = RunResult()
    try:
        client = connection(conn, resolved)
    except OgonggoUnavailable as exc:
        result.reason = str(exc)
        if report and not manual:
            await work_report.report(REPORT_NAME, [result.reason], ok=False)
        return result

    page_fetcher = fetcher or get_fetcher()
    own_renderer = _Renderer() if renderer is None else None
    page_renderer = renderer or own_renderer
    verdicts: dict[str, asyncio.Task[Verdict]] = {}
    gate = asyncio.Semaphore(CONCURRENCY)

    async def check(target: Target) -> Checked:
        url = fetch_url(target.source_url)
        task = verdicts.get(url)
        if task is None:

            async def run() -> Verdict:
                async with gate:
                    return await judge(
                        conn,
                        url,
                        title=target.title,
                        company=target.company,
                        fetcher=page_fetcher,
                        renderer=page_renderer,
                        settings=resolved,
                    )

            task = asyncio.ensure_future(run())
            verdicts[url] = task
        verdict = await task
        item = Checked(target, verdict)
        logger.info(
            "원문 확인 job=%s %s %s — %s",
            target.job_id,
            verdict.status,
            target.source_url,
            verdict.reason,
        )
        if verdict.status == CLOSED and close:
            item.close_error = await ogonggo.close(client, target.job_id)
            if not item.close_error:
                result.closed += 1
        return item

    after_id = 0 if manual else read_cursor(conn)
    try:
        async with client:
            while True:
                if _past(stop_at):
                    break
                size = PAGE_SIZE if limit is None else min(PAGE_SIZE, limit - len(result.checked))
                if size <= 0:
                    break
                page = await ogonggo.targets(client, after_id, size)
                if not page:
                    result.finished = True
                    break
                if _past(stop_at):
                    break
                result.checked += await asyncio.gather(*(check(target) for target in page))
                after_id = page[-1].job_id
                if not manual:
                    write_cursor(conn, after_id)
    except OgonggoUnavailable as exc:
        result.reason = str(exc)
    finally:
        if own_renderer is not None:
            await own_renderer.aclose()

    if result.finished and not manual:
        write_cursor(conn, 0)
    if report and not manual:
        await work_report.report(REPORT_NAME, report_lines(result), ok=not result.reason)
    return result


def report_lines(result: RunResult) -> list[str]:
    lines = [
        f"확인 {len(result.checked)}건 · 모집 중 {result.count(OPEN)} · 마감 {result.closed}"
        f" · 모르겠음 {result.count(UNKNOWN)}"
    ]
    if result.reason:
        lines.append(f"멈춤: {result.reason}")
    elif not result.finished:
        lines.append("멈춤 시각이 되어 남은 공고는 다음에 이어서 본다")
    failed = [item for item in result.checked if item.close_error]
    if failed:
        lines.append(f"마감 실패 {len(failed)}건: {failed[0].close_error}")
    closed = [
        item for item in result.checked if item.verdict.status == CLOSED and not item.close_error
    ]
    for item in closed[:REPORT_CLOSED_LINES]:
        lines.append(
            f"마감 {item.target.job_id} {item.target.company} · {item.target.title}"
            f" — {item.verdict.reason}"
        )
    if len(closed) > REPORT_CLOSED_LINES:
        lines.append(f"그 밖 {len(closed) - REPORT_CLOSED_LINES}건")
    unknown = Counter(
        _reason_kind(item.verdict.reason)
        for item in result.checked
        if item.verdict.status == UNKNOWN
    )
    if unknown:
        lines.append(
            "모르겠음 사유: "
            + ", ".join(f"{kind} {count}" for kind, count in unknown.most_common(5))
        )
    return lines


def _reason_kind(reason: str) -> str:
    """사유를 묶어 세려고 앞부분만 쓴다. 주소처럼 공고마다 다른 부분은 뗀다."""
    return reason.split(":", 1)[0].split("(", 1)[0].strip()[:30] or "사유 없음"


def _past(stop_at: datetime | None) -> bool:
    return stop_at is not None and datetime.now(stop_at.tzinfo) >= stop_at
