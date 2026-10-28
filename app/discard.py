"""공고 폐기. 공고의 행을 비우고 주소를 남겨 다음 수집이 다시 담지 않게 한다.

두 길이 이 함수를 쓴다.

- 운영자가 공고 목록에서 지운다 (`app/api/review_filter.py`, 2026-10-21, 0061). 지우면 다음 날 같은
  공고가 새 공고로 다시 들어와 같은 것을 여러 번 지웠다
- AI 분류가 직군을 대상 직군 밖으로 정한다 (`off_target_ids`, 2026-10-22, 0062). 수집은 애매하면
  담으므로(`app/crawler/screen.py`) 대상 밖 공고가 섞여 들어오고, 보내지 않을 뿐 목록에 남아 확인
  필요를 채웠다. 분류가 직군을 정한 뒤에는 애매하지 않다

남긴 주소는 `discarded_jobs` 에 있고 수집은 그 주소를 아는 공고로 본다 (`app/crawler/runner.py` 의
`_is_known`). 다시 받으려면 그 행을 지운다.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence

from app.crawler import target_fields

OPERATOR = "operator"
OFF_TARGET = "off_target"

# `IN (?, ?, ...)` 에 한 번에 넣을 id 수. SQLite 의 바인딩 개수 상한에 걸리지 않게 끊는다
_ID_CHUNK = 500


def chunks(ids: Sequence[int]) -> Iterator[tuple[list[int], str]]:
    """id 목록을 바인딩 가능한 크기로 끊는다. 묶음마다 물음표 자리도 함께 낸다."""
    for start in range(0, len(ids), _ID_CHUNK):
        part = list(ids[start : start + _ID_CHUNK])
        yield part, ",".join("?" for _ in part)


def discard(
    conn: sqlite3.Connection, raw_job_ids: Sequence[int], *, reason: str = OPERATOR
) -> tuple[int, int, int]:
    """공고를 지우고 주소를 폐기로 남긴다. (수집 건, 정규화 행, 사람 보정) 지운 수를 돌려준다.

    여러 표를 한 트랜잭션으로, 외래키 순서대로 비운다. `job_field_overrides` ->
    `job_classifications` -> `job_field_suggestions` -> `normalized_jobs` -> `raw_job_history` ->
    `raw_jobs` 순이다. 거꾸로 지우면 외래키가 막는다. 분류·제안은 원문에서 다시 만들 수 있는
    파생값이라 반환값 개수에 넣지 않는다. 한 트랜잭션인 이유는 절반만 지워진 상태를 손으로 풀 수
    없어서다.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        overrides = normalized = raw = 0
        for part, marks in chunks(raw_job_ids):
            overrides += conn.execute(
                f"DELETE FROM job_field_overrides WHERE raw_job_id IN ({marks})", part
            ).rowcount
            conn.execute(f"DELETE FROM job_custom_values WHERE raw_job_id IN ({marks})", part)
            conn.execute(f"DELETE FROM job_classifications WHERE raw_job_id IN ({marks})", part)
            conn.execute(f"DELETE FROM job_field_suggestions WHERE raw_job_id IN ({marks})", part)
            conn.execute(f"DELETE FROM quality_gate_runs WHERE raw_job_id IN ({marks})", part)
            normalized += conn.execute(
                f"DELETE FROM normalized_jobs WHERE raw_job_id IN ({marks})", part
            ).rowcount
            # 원문 다시 수집이 남긴 이전 값. 공고를 지우면 그 이력도 가리킬 곳이 없다
            conn.execute(f"DELETE FROM raw_job_history WHERE raw_job_id IN ({marks})", part)
            conn.execute(
                "INSERT OR IGNORE INTO discarded_jobs (workflow_id, source_url, title, reason)"
                " SELECT workflow_id, source_url,"
                " coalesce(json_extract(raw_data_json, '$.title'), ''), ?"
                f" FROM raw_jobs WHERE id IN ({marks})",
                [reason, *part],
            )
            raw += conn.execute(f"DELETE FROM raw_jobs WHERE id IN ({marks})", part).rowcount
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return raw, normalized, overrides


def off_target_ids(conn: sqlite3.Connection, raw_job_ids: Sequence[int] | None = None) -> list[int]:
    """AI 분류가 직군을 대상 직군 밖으로 정한 공고. `raw_job_ids` 를 주면 그 안에서만 고른다.

    - 직무마다 나뉜 공고는 모든 번호가 대상 밖일 때만이다. 하나라도 대상이면 그 번호는 보낸다
    - 직군이 빈 공고는 버리지 않는다. 분류가 직군을 못 정한 것이지 대상 밖으로 정한 것이 아니다
    - 이미 오공고로 보낸 공고와 직접 추가한 공고는 버리지 않는다. 사람이 고르고 보낸 것이다
    - 대상 직군을 모두 켜 두었으면 아무것도 고르지 않는다
    """
    # 수집 모듈이 분류를 거쳐 이 모듈을 읽는다
    from app.crawler.manual import MANUAL_KIND

    fields = target_fields.filtering_fields(conn)
    if not fields:
        return []
    listed = ", ".join("?" for _ in fields)
    sql = (
        "SELECT r.id FROM raw_jobs r JOIN workflows w ON w.id = r.workflow_id"
        " WHERE coalesce(w.kind, '') != ?"
        " AND EXISTS (SELECT 1 FROM job_classifications c WHERE c.raw_job_id = r.id)"
        " AND EXISTS (SELECT 1 FROM normalized_jobs n WHERE n.raw_job_id = r.id)"
        " AND NOT EXISTS (SELECT 1 FROM normalized_jobs n WHERE n.raw_job_id = r.id"
        f" AND (trim(coalesce(n.job_field, '')) = '' OR n.job_field IN ({listed})))"
        " AND NOT EXISTS (SELECT 1 FROM normalized_jobs n JOIN spring_deliveries d"
        " ON d.source_url = n.source_url AND d.status = 'sent' WHERE n.raw_job_id = r.id)"
    )
    params: list[object] = [MANUAL_KIND, *fields]
    if raw_job_ids is None:
        return [int(row[0]) for row in conn.execute(sql + " ORDER BY r.id", params)]
    found: list[int] = []
    for part, marks in chunks(raw_job_ids):
        rows = conn.execute(sql + f" AND r.id IN ({marks}) ORDER BY r.id", [*params, *part])
        found.extend(int(row[0]) for row in rows)
    return found
