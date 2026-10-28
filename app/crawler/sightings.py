"""상시 채용 공고가 사이트 목록에 아직 있는지 적고, 연달아 안 보이면 마감으로 본다.

2026-10-28 결정. 상시 채용에는 마감일이 없어 원문이 내려간 공고도 계속 모집 중으로 남는다. 매일
수집이 목록을 한 번 읽으므로 그 목록과 견준다 — 사이트를 따로 더 부르지 않는다. 기간 채용은 보지
않는다. 마감일이 지나면 오공고가 매시 스스로 마감한다.

## 판정하는 실행

안 보이는 것을 마감으로 읽으려면 그날 목록을 다 읽었다고 믿을 수 있어야 한다. 아래 중 하나라도
아니면 이 실행으로는 아무것도 바꾸지 않는다 — 횟수를 올리지도, 0 으로 되돌리지도 않는다.

| 조건 | 이유 |
|---|---|
| 사이트가 `list_complete` | 첫 쪽만 읽는 사이트에서 안 보이는 것은 다음 쪽으로 밀린 것이다 |
| 매일 수집(`schedule`) | 수동·시험·다시 수집은 하루 한 번이 아니라 횟수가 날짜를 뜻하지 않는다 |
| 실행이 성공 | 실패한 날의 목록은 믿을 수 없다 |
| 목록 단계 실패가 0건 | 값을 못 읽은 항목이 있으면 그 공고가 빠졌을 수 있다 |
| 첫 실행 상한에 걸리지 않음 | 상한이 목록을 자른 실행이다 |
| 목록 수가 평소의 절반 이상 | 셀렉터가 반쯤 깨진 날을 막는다. 견줄 실행이 모자라도 하지 않는다 |

## 다시 보인 공고

드문 경로라 러프하게 다룬다. 횟수를 0 으로 되돌리고 마감 판정을 비운다. 로그만 남긴다.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from statistics import median
from typing import TYPE_CHECKING

from app.crawler.failures import SUCCESS

if TYPE_CHECKING:
    from app.crawler.runner import RunResult

logger = logging.getLogger(__name__)

# 연달아 이만큼 안 보이면 마감으로 본다. 매일 한 번 도므로 사흘이다
MISS_LIMIT = 3
# 목록 수를 견줄 최근 실행 수와, 그중 있어야 하는 최소 수
BASELINE_RUNS = 7
BASELINE_MIN = 3
# 목록 수가 평소의 이 비율보다 적으면 판정하지 않는다
MATCHED_FLOOR = 0.5

# 이 워크플로우의 상시 채용 공고 주소. 분류가 끝나 모집 유형이 정해진 공고만 본다
_ALWAYS_OPEN_SQL = """
    SELECT DISTINCT r.source_url
      FROM raw_jobs r
     WHERE r.workflow_id = ?
       AND EXISTS (SELECT 1 FROM normalized_jobs n
                    WHERE n.raw_job_id = r.id AND n.recruitment_type = 'ALWAYS_OPEN')
"""


@dataclass(frozen=True)
class SightingResult:
    """한 실행의 판정. `reason` 이 있으면 판정하지 않은 것이고 나머지는 0 이다."""

    reason: str = ""
    seen: int = 0
    missed: int = 0
    closed: int = 0
    reopened: int = 0


def record(
    conn: sqlite3.Connection,
    workflow_id: int,
    result: RunResult,
    *,
    trigger: str,
    capped: bool,
) -> SightingResult:
    """실행 하나의 목록으로 상시 채용 공고를 본 기록을 갱신한다."""
    reason = _skip_reason(conn, workflow_id, result, trigger=trigger, capped=capped)
    if reason:
        logger.info("workflow %s: 상시 채용 대조를 건너뛴다 — %s", workflow_id, reason)
        return SightingResult(reason=reason)

    shown = set(result.seen_links)
    urls = [str(row[0]) for row in conn.execute(_ALWAYS_OPEN_SQL, (workflow_id,))]
    seen = [url for url in urls if url in shown]
    missed = [url for url in urls if url not in shown]

    conn.execute("BEGIN IMMEDIATE")
    try:
        reopened = conn.execute(
            """
            SELECT count(*) FROM job_sightings
             WHERE workflow_id = ? AND closed_at IS NOT NULL
               AND source_url IN (SELECT value FROM json_each(?))
            """,
            (workflow_id, json.dumps(seen, ensure_ascii=False)),
        ).fetchone()[0]
        conn.executemany(
            """
            INSERT INTO job_sightings (workflow_id, source_url, last_seen_at, missed_runs)
            VALUES (?, ?, datetime('now'), 0)
            ON CONFLICT (workflow_id, source_url) DO UPDATE
               SET last_seen_at = excluded.last_seen_at, missed_runs = 0, closed_at = NULL,
                   updated_at = datetime('now')
            """,
            [(workflow_id, url) for url in seen],
        )
        conn.executemany(
            """
            INSERT INTO job_sightings (workflow_id, source_url, missed_runs)
            VALUES (?, ?, 1)
            ON CONFLICT (workflow_id, source_url) DO UPDATE
               SET missed_runs = missed_runs + 1, updated_at = datetime('now')
            """,
            [(workflow_id, url) for url in missed],
        )
        closed = conn.execute(
            """
            UPDATE job_sightings SET closed_at = datetime('now')
             WHERE workflow_id = ? AND closed_at IS NULL AND missed_runs >= ?
            """,
            (workflow_id, MISS_LIMIT),
        ).rowcount
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    outcome = SightingResult(
        seen=len(seen), missed=len(missed), closed=closed, reopened=int(reopened)
    )
    logger.info(
        "workflow %s: 상시 채용 대조 seen=%s missed=%s closed=%s reopened=%s",
        workflow_id,
        outcome.seen,
        outcome.missed,
        outcome.closed,
        outcome.reopened,
    )
    return outcome


def _skip_reason(
    conn: sqlite3.Connection,
    workflow_id: int,
    result: RunResult,
    *,
    trigger: str,
    capped: bool,
) -> str:
    """이 실행으로 판정하지 않는 이유. 판정해도 되면 빈 문자열이다."""
    # 순환 임포트를 피해 여기서 읽는다. 러너가 이 모듈을 부른다
    from app.crawler.runner import SCHEDULE

    row = conn.execute(
        "SELECT c.list_complete FROM workflows w JOIN crawlers c ON c.id = w.crawler_id"
        " WHERE w.id = ?",
        (workflow_id,),
    ).fetchone()
    if row is None or not row["list_complete"]:
        return "목록이 다 보이는 사이트로 켜지 않았다"
    if trigger != SCHEDULE:
        return f"매일 수집이 아니다 ({trigger})"
    if result.status != SUCCESS:
        return f"실행이 성공하지 않았다 ({result.status})"
    if result.list_misses:
        return f"목록에서 {result.list_misses}건을 읽지 못했다"
    if capped:
        return "첫 실행 상한이 목록을 잘랐다"

    previous = [
        int(r["matched"])
        for r in conn.execute(
            """
            SELECT matched FROM crawl_runs
             WHERE workflow_id = ? AND id <> ? AND status = 'success'
               AND trigger = 'schedule' AND matched IS NOT NULL
             ORDER BY id DESC LIMIT ?
            """,
            (workflow_id, result.run_id, BASELINE_RUNS),
        )
    ]
    if len(previous) < BASELINE_MIN:
        return f"견줄 최근 실행이 {len(previous)}번뿐이다 ({BASELINE_MIN}번 필요)"
    usual = median(previous)
    if result.matched < usual * MATCHED_FLOOR:
        return f"목록 수 {result.matched}건이 평소 {usual:g}건의 절반보다 적다"
    return ""
