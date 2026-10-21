"""부실한 공고 다시 읽기. 수집은 됐는데 본문이 거의 없는 공고를 페이지 전체로 다시 읽는다.

2026-10-21 결정. 본문이 200자 미만인 공고가 확인 필요(`app/api/review_filter.py` 의 `본문 짧음`)에
쌓여 운영자가 원문을 직접 붙여 넣거나 셀렉터를 고쳤다. 대개 셋 중 하나다.

- 상세 셀렉터가 본문이 아닌 영역(제목 줄, 버튼)을 잡았다
- 본문을 JS 가 그려 정적 HTML 에는 글자가 없다
- 공고가 이미지 한 장이다

셋 다 `공고 한 건 추가` 가 이미 푸는 문제다 (`app/crawler/manual.py` 의 `read_page`). 셀렉터 없이
페이지 본문을 펴고, 글자가 모자라면 브라우저로 렌더하고, 본문이 짧으면 이미지를 읽는다. 그 길로
다시 읽어 더 긴 본문을 얻으면 같은 행에 갈아 끼운다.

## 지키는 것

- **지금 값을 지우지 않는다.** 원문 다시 수집처럼 갈아 끼우기 전 값을 `raw_job_history` 에 남긴다
  (`app/crawler/recollect.py`). 공고 번호와 수집 시각은 그대로다
- **본문 칸만 바꾼다.** 제목·회사·마감일처럼 셀렉터가 읽은 다른 칸은 그대로다 — 페이지 전체를 편
  글에서는 그 칸을 가를 수 없다. 분류가 새 원문을 읽고 나머지를 채운다
- **더 나을 때만 바꾼다.** 다시 읽은 본문이 200자를 넘고 지금 본문보다 길 때만이다
- **공고마다 한 번이다.** 원래 짧은 공고가 있다. `posting_enrichments` 에 적고 다시 열지 않는다
- **직접 추가한 공고는 보지 않는다.** 이미 이 길로 읽었거나 운영자가 원문을 넣은 공고다

## 언제 도는가

매일 수집이 끝나고 분류를 걸기 전이다 (`app/crawler/runner.py` 의
`run_workflow`). 분류가 짧은 본문을 먼저 읽고 끝나면 다시 분류해야 해서 그 앞에 둔다. 한 실행에서
`MAX_PER_RUN` 건까지만 연다 — 사이트가 통째로 짧게 읽히면 셀렉터 문제라 모두 열어도 같다. 이미
쌓인 공고는 `python -m app.cli enrich backlog` 로 돈다.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

from app.crawler.fetcher import FetchPolicy, PageSource, get_fetcher
from app.crawler.hashing import content_hash
from app.crawler.images import ImageReader, LlmImageReader
from app.crawler.manual import MANUAL_KIND, ManualAddError, RenderOpener, read_page
from app.crawler.playwright import Renderer
from app.normalize.backfill import rewrite_one
from app.normalize.engine import NormalizeError, load_rules

logger = logging.getLogger(__name__)

# 확인 필요의 `본문 짧음` 과 같은 기준이다
SHORT_BODY_CHARS = 200
# 한 실행에서 다시 읽는 공고 수
MAX_PER_RUN = 10

ENRICHED = "enriched"
UNCHANGED = "unchanged"
ERROR = "error"


@dataclass(frozen=True)
class Enriched:
    raw_job_id: int
    outcome: str
    before_chars: int
    after_chars: int | None = None
    message: str = ""


def _length(text: str) -> int:
    return len(" ".join(text.split()))


def short_ids(conn: sqlite3.Connection, raw_job_ids: Sequence[int] | None = None) -> list[int]:
    """다시 읽을 공고. 본문이 짧고, 사이트에서 모았고, 아직 다시 읽지 않은 것이다.

    `raw_job_ids` 를 주면 그 안에서만 고르고, 없으면 쌓인 공고 전부에서 고른다. 직무마다 나뉜
    공고는 수집 한 건이라 한 번만 나온다.
    """
    where = ""
    params: list[int] = []
    if raw_job_ids is not None:
        if not raw_job_ids:
            return []
        where = f" AND r.id IN ({', '.join('?' for _ in raw_job_ids)})"
        params = list(raw_job_ids)
    rows = conn.execute(
        "SELECT DISTINCT r.id FROM raw_jobs r"
        " JOIN workflows w ON w.id = r.workflow_id"
        " JOIN normalized_jobs n ON n.raw_job_id = r.id AND n.part = 1"
        f" WHERE coalesce(w.kind, '') != '{MANUAL_KIND}'"
        f" AND length(trim(coalesce(n.body, ''))) < {SHORT_BODY_CHARS}"
        " AND NOT EXISTS (SELECT 1 FROM posting_enrichments e WHERE e.raw_job_id = r.id)"
        f"{where} ORDER BY r.id",
        params,
    ).fetchall()
    return [int(row[0]) for row in rows]


async def enrich_ids(
    conn: sqlite3.Connection,
    raw_job_ids: Sequence[int],
    *,
    run_id: int | None = None,
    fetcher: FetchPolicy | None = None,
    reader: ImageReader | None = None,
) -> list[Enriched]:
    """고른 공고를 다시 읽는다. 브라우저는 필요할 때 한 번만 띄워 함께 쓴다. 예외를 올리지 않는다.

    `run_id` 는 이력에 남길 실행 번호다. 쌓인 공고를 따로 돌릴 때는 없다.
    """
    if not raw_job_ids:
        return []
    resolved = fetcher or get_fetcher()
    image_reader = reader if reader is not None else LlmImageReader(conn)
    renderer: Renderer | None = None

    async def open_renderer() -> PageSource:
        nonlocal renderer
        if renderer is None:
            renderer = Renderer(resolved)
        return renderer

    results: list[Enriched] = []
    try:
        for raw_job_id in raw_job_ids:
            try:
                result = await _enrich_one(
                    conn, raw_job_id, run_id, resolved, open_renderer, image_reader
                )
            except Exception as exc:  # noqa: BLE001 — 한 건이 죽어도 수집 실행은 이어진다
                logger.exception("다시 읽기: raw_jobs %s 에서 예외", raw_job_id)
                result = Enriched(raw_job_id, ERROR, 0, message=f"{type(exc).__name__}: {exc}")
            _note(conn, result)
            results.append(result)
    finally:
        if renderer is not None:
            await renderer.aclose()
    enriched = sum(result.outcome == ENRICHED for result in results)
    logger.info("다시 읽기: %s건 중 %s건 본문을 갈아 끼웠다", len(results), enriched)
    return results


async def after_run(
    conn: sqlite3.Connection,
    run_id: int,
    stored_ids: Sequence[int],
    *,
    fetcher: FetchPolicy | None = None,
) -> list[Enriched]:
    """수집 실행이 이번에 담은 공고 중 짧은 것을 다시 읽는다. 분류를 걸기 전에 부른다."""
    picked = short_ids(conn, stored_ids)[:MAX_PER_RUN]
    return await enrich_ids(conn, picked, run_id=run_id, fetcher=fetcher)


async def _enrich_one(
    conn: sqlite3.Connection,
    raw_job_id: int,
    run_id: int | None,
    fetcher: FetchPolicy,
    open_renderer: RenderOpener,
    reader: ImageReader,
) -> Enriched:
    row = conn.execute(
        "SELECT source_url, raw_data_json FROM raw_jobs WHERE id = ?", (raw_job_id,)
    ).fetchone()
    record: dict[str, str] = json.loads(row["raw_data_json"] or "{}")
    before = _length(record.get("body", ""))
    try:
        detail, _ = await read_page(str(row["source_url"]), fetcher, open_renderer, reader)
    except ManualAddError as exc:
        return Enriched(raw_job_id, ERROR, before, message=str(exc))
    body = detail.fields["body"]
    after = _length(body)
    if after < SHORT_BODY_CHARS or after <= before:
        return Enriched(raw_job_id, UNCHANGED, before, after, "다시 읽어도 더 긴 본문이 없다")

    record = {**record, "body": body, "source_text": detail.source_text}
    if detail.body_html.strip():
        record["body_html"] = detail.body_html
    _replace(conn, raw_job_id, record, run_id)
    try:
        rewrite_one(conn, raw_job_id, load_rules(conn))
    except NormalizeError as exc:
        return Enriched(
            raw_job_id, ENRICHED, before, after, f"본문은 갈아 끼웠으나 정규화하지 못했다: {exc}"
        )
    return Enriched(raw_job_id, ENRICHED, before, after, f"본문 {before}자 → {after}자")


def _replace(
    conn: sqlite3.Connection, raw_job_id: int, record: dict[str, str], run_id: int | None
) -> None:
    """지금 값을 이력에 남기고 같은 행에 새 값을 넣는다. 한 트랜잭션이다."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO raw_job_history (raw_job_id, run_id, raw_data_json, content_hash,"
            " crawled_at) SELECT id, ?, raw_data_json, content_hash, crawled_at"
            " FROM raw_jobs WHERE id = ?",
            (run_id, raw_job_id),
        )
        conn.execute(
            "UPDATE raw_jobs SET raw_data_json = ?, content_hash = ? WHERE id = ?",
            (json.dumps(record, ensure_ascii=False), content_hash(record), raw_job_id),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _note(conn: sqlite3.Connection, result: Enriched) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO posting_enrichments"
        " (raw_job_id, outcome, before_chars, after_chars, message) VALUES (?, ?, ?, ?, ?)",
        (
            result.raw_job_id,
            result.outcome,
            result.before_chars,
            result.after_chars,
            result.message,
        ),
    )
