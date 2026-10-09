"""렛츠커리어 콘텐츠 태그 한 번 (2026-10-09 결정, LC-3448).

1. 오공고 서버에서 태그할 콘텐츠를 `MAX_PER_RUN` 건까지 받는다
2. 콘텐츠마다 AI 로 태그를 고르고 서버로 보낸다
3. 멈춤 시각이 지나거나 AI 가 연달아 `MAX_FAILURES_IN_ROW` 번 실패하면 멈춘다

콘텐츠는 수백 건이고 잘 바뀌지 않아 공고 분석처럼 화면·기록 테이블을 두지 않는다. 결과는 로그와
`llm_calls` 에 남는다. 실패한 콘텐츠는 서버가 다음 실행에도 대상으로 내준다.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from app.config import Settings
from app.job_analysis import ogonggo as job_analysis_ogonggo
from app.letscareer_tags import ogonggo
from app.letscareer_tags.tagger import TagError, tag

logger = logging.getLogger(__name__)

MAX_PER_RUN = 200
MAX_FAILURES_IN_ROW = 5


@dataclass
class RunSummary:
    status: str = "success"
    targets: int = 0
    sent: int = 0
    failed: int = 0
    left: int = 0
    notes: list[str] = field(default_factory=list)


async def run_once(
    conn: sqlite3.Connection,
    *,
    settings: Settings | None = None,
    stop_at: datetime | None = None,
    llm_client: object | None = None,
    max_per_run: int = MAX_PER_RUN,
) -> RunSummary:
    summary = RunSummary()
    try:
        async with job_analysis_ogonggo.connection(conn, settings) as client:
            found = await ogonggo.targets(client, max_per_run)
            summary.targets = len(found)
            failures_in_row = 0
            for index, target in enumerate(found):
                if stop_at is not None and datetime.now(stop_at.tzinfo) >= stop_at:
                    summary.left = len(found) - index
                    summary.notes.append(
                        f"멈춤 시각({stop_at:%H:%M})이 되어 {summary.left}건은 다음 실행에 넘겼다"
                    )
                    break
                if failures_in_row >= MAX_FAILURES_IN_ROW:
                    summary.left = len(found) - index
                    summary.status = "failed"
                    summary.notes.append(f"AI 가 {MAX_FAILURES_IN_ROW}번 연달아 실패해 멈췄다")
                    break
                try:
                    result = await tag(conn, target, settings=settings, client=llm_client)
                except TagError as exc:
                    summary.failed += 1
                    failures_in_row += 1
                    logger.warning("렛츠커리어 콘텐츠 %s 태그 실패: %s", target.content_id, exc)
                    continue
                failures_in_row = 0
                if result.dropped:
                    logger.info(
                        "렛츠커리어 콘텐츠 %s 의 목록 밖 태그를 버렸다: %s",
                        target.content_id,
                        result.dropped,
                    )
                sent = await ogonggo.put(
                    client, target.content_id, content_hash=target.content_hash, tags=result.tags
                )
                if sent.outcome == ogonggo.SENT:
                    summary.sent += 1
                else:
                    summary.failed += 1
                    logger.warning(
                        "렛츠커리어 콘텐츠 %s 태그 전송 실패: %s", target.content_id, sent.message
                    )
    except ogonggo.OgonggoUnavailable as exc:
        summary.status = "failed"
        summary.notes.append(str(exc))
    except Exception as exc:
        logger.exception("렛츠커리어 콘텐츠 태그가 예외로 끝났다")
        summary.status = "failed"
        summary.notes.append(f"{type(exc).__name__}: {exc}")
    logger.info(
        "렛츠커리어 콘텐츠 태그 %s: 대상 %s, 보냄 %s, 실패 %s, 남음 %s %s",
        summary.status,
        summary.targets,
        summary.sent,
        summary.failed,
        summary.left,
        " / ".join(summary.notes),
    )
    return summary
