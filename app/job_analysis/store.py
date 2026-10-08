"""공고 분석 기록 (`job_analysis_records`) 을 읽고 쓴다.

오공고 공고 하나가 행 하나다. 다시 분석하면 같은 행을 덮어쓴다 — 화면이 보여 주는 것은 공고마다
지금 상태다. 지난 분석 비용은 `llm_calls` 에 남는다.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

SENT = "sent"
UNSENT = "unsent"
FAILED = "failed"
STALE = "stale"
STATUSES: tuple[tuple[str, str], ...] = (
    (SENT, "보냄"),
    (UNSENT, "못 보냄"),
    (FAILED, "분석 실패"),
    (STALE, "본문 바뀜"),
)
STATUS_LABELS: dict[str, str] = dict(STATUSES)
PAGE_SIZE = 50


@dataclass(frozen=True)
class Analyzed:
    """분석 한 번의 결과. 저장할 때 쓴다."""

    analysis: dict[str, Any]
    guide_version: int | None
    model: str
    input_tokens: int
    output_tokens: int


def get(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM job_analysis_records WHERE ogonggo_job_id = ?", (job_id,)
    ).fetchone()
    return row


def get_by_id(conn: sqlite3.Connection, record_id: int) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM job_analysis_records WHERE id = ?", (record_id,)
    ).fetchone()
    return row


def save_target(
    conn: sqlite3.Connection,
    job_id: int,
    source: str,
    job: dict[str, Any],
    content_hash: str,
) -> None:
    """서버가 준 공고를 행에 적는다. 이미 있으면 공고 글과 해시만 바꾼다."""
    conn.execute(
        """
        INSERT INTO job_analysis_records
               (ogonggo_job_id, source, company, title, source_url, posting_json, content_hash,
                status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'failed')
        ON CONFLICT (ogonggo_job_id) DO UPDATE SET
               source = excluded.source, company = excluded.company, title = excluded.title,
               source_url = excluded.source_url, posting_json = excluded.posting_json,
               content_hash = excluded.content_hash, updated_at = datetime('now')
        """,
        (
            job_id,
            source,
            str(job.get("companyName") or ""),
            str(job.get("title") or ""),
            str(job.get("sourceUrl") or ""),
            json.dumps(job, ensure_ascii=False),
            content_hash,
        ),
    )


def save_analysis(
    conn: sqlite3.Connection, job_id: int, analyzed: Analyzed, run_id: int | None
) -> None:
    conn.execute(
        """
        UPDATE job_analysis_records
           SET analysis_json = ?, guide_version = ?, model = ?, input_tokens = ?,
               output_tokens = ?, status = 'unsent', error = '', attempts = attempts + 1,
               run_id = ?, analyzed_at = datetime('now'), updated_at = datetime('now')
         WHERE ogonggo_job_id = ?
        """,
        (
            json.dumps(analyzed.analysis, ensure_ascii=False),
            analyzed.guide_version,
            analyzed.model,
            analyzed.input_tokens,
            analyzed.output_tokens,
            run_id,
            job_id,
        ),
    )


def mark(
    conn: sqlite3.Connection,
    job_id: int,
    status: str,
    error: str = "",
    *,
    run_id: int | None = None,
    counted: bool = False,
) -> None:
    """상태를 바꾼다. 보냈으면 보낸 시각을 적는다. `counted` 면 시도 수를 올린다(분석 실패)."""
    conn.execute(
        f"""
        UPDATE job_analysis_records
           SET status = ?, error = ?, run_id = coalesce(?, run_id),
               sent_at = CASE WHEN ? = 'sent' THEN datetime('now') ELSE sent_at END,
               attempts = attempts + {1 if counted else 0}, updated_at = datetime('now')
         WHERE ogonggo_job_id = ?
        """,
        (status, error[:1000], run_id, status, job_id),
    )


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, count(*) AS n FROM job_analysis_records GROUP BY status"
    ).fetchall()
    found = {str(row["status"]): int(row["n"]) for row in rows}
    return {status: found.get(status, 0) for status, _ in STATUSES}


def today(conn: sqlite3.Connection, since_utc: str) -> dict[str, Any]:
    """오늘(표시 시간대 자정 이후) 분석·전송 수와 토큰."""
    row = conn.execute(
        """
        SELECT count(*) FILTER (WHERE analyzed_at >= ?) AS analyzed,
               count(*) FILTER (WHERE sent_at >= ?) AS sent,
               count(*) FILTER (WHERE status = 'failed' AND updated_at >= ?) AS failed
          FROM job_analysis_records
        """,
        (since_utc, since_utc, since_utc),
    ).fetchone()
    return {
        "analyzed": int(row["analyzed"]),
        "sent": int(row["sent"]),
        "failed": int(row["failed"]),
    }


def listing(
    conn: sqlite3.Connection,
    *,
    status: str = "",
    source: str = "",
    query: str = "",
    page: int = 1,
) -> tuple[list[sqlite3.Row], int]:
    """화면 목록. 최근에 바뀐 것부터다. 둘째 값은 거른 뒤 전체 수다."""
    where: list[str] = []
    params: list[Any] = []
    if status:
        where.append("status = ?")
        params.append(status)
    if source:
        where.append("source = ?")
        params.append(source)
    if query:
        where.append("(company LIKE ? OR title LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%"])
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    total = int(
        conn.execute(f"SELECT count(*) FROM job_analysis_records{clause}", params).fetchone()[0]
    )
    rows = conn.execute(
        f"SELECT * FROM job_analysis_records{clause} ORDER BY updated_at DESC, id DESC"
        " LIMIT ? OFFSET ?",
        [*params, PAGE_SIZE, (max(page, 1) - 1) * PAGE_SIZE],
    ).fetchall()
    return rows, total


def analysis_of(row: sqlite3.Row) -> dict[str, Any] | None:
    text = row["analysis_json"]
    if not text:
        return None
    data = json.loads(str(text))
    return data if isinstance(data, dict) else None


def posting_of(row: sqlite3.Row) -> dict[str, Any]:
    data = json.loads(str(row["posting_json"]))
    return data if isinstance(data, dict) else {}
