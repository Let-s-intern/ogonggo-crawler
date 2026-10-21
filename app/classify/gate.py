"""품질 검사. 분류가 끝난 공고가 오공고로 갈 만한지 보고, 아니면 AI 로 다시 분류한다.

2026-10-21 결정. 확인 필요 공고가 너무 많아 운영자가 손으로 `AI로 다시 채우기` 를 누르던 것을
자동으로 돌린다. 흐름은 이렇다.

```
수집 → AI 분류 → 품질 검사 ─ 통과 → 오공고 전송
                    │
                    └ 미통과 → AI 재분류(최대 2번) → 품질 검사
                                 └ 2번 뒤에도 미통과 → 확인 필요 (대상 직군 밖은 전송 제외)
```

빈 칸을 규칙으로 채우지 않는다 — 모두 AI 가 한다 (`app/classify/batch.py` 의 `recheck_ids`).

## 네 조건과 끝내 못 풀었을 때

| 조건 | 무엇 | 끝내 못 풀면 |
|---|---|---|
| `empty_key` | 직무·마감일·고용 형태·경력 구분 중 하나라도 빔. 상시 채용도 본다 | 확인 필요 |
| `thin_content` | 공고 내용 여덟 칸(`CONTENT_FIELDS`) 중 넷 이상 빔 | 확인 필요 |
| `off_target` | 직군이 대상 직군(`app/crawler/target_fields.py`)이 아님 | 전송 제외 |
| `career_cap` | 1·2년차가 지원할 수 있는데 최대 경력 연수(0056)를 모름 | 확인 필요 |

확인 필요로 가는 셋(`BLOCKING`)에 걸린 공고는 자동 전송하지 않는다 (`blocked_sql`). 대상 직군 밖은
원래 전송의 직군 조건(`app/deliver/spring.py` 의 `target_sql`)이 막는다. 사람이 골라 보내는 길은
막지 않는다.

## 공고마다 두 번까지

다시 돌려도 그대로인 공고가 있다 — 원문에 마감일이 없는 상시 채용, 상한을 말하지 않는 경력 공고.
그래서 다시 분류한 횟수를 `quality_gate_runs.attempts` 에 세고 `MAX_RECHECKS` 를 넘기지 않는다
(`migrations/0057`). 처음 걸린 조건은 `reasons`, 마지막 재분류 뒤에도 걸린 조건은 `reasons_after`
다. 검사가 얼마나 덜어 주는지 이 둘로 센다.

## 보낼 공고만 본다

이미 오공고로 보낸 공고와 마감이 지난 공고는 보지 않는다. 확인 필요(`app/api/review_filter.py`)와
같은 범위다 — 다시 분류해도 보내지 않을 공고에 돈을 쓰지 않는다.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from app.crawler import target_fields

# 조건 이름과 화면 이름. 이름이 `quality_gate_runs.reasons` 에 그대로 저장된다
EMPTY_KEY = "empty_key"
THIN_CONTENT = "thin_content"
OFF_TARGET = "off_target"
CAREER_CAP = "career_cap"
REASONS: dict[str, str] = {
    EMPTY_KEY: "직무·마감일·고용 형태·경력 구분 빔",
    THIN_CONTENT: "공고 내용 절반 이상 빔",
    OFF_TARGET: "대상 직군 아님",
    CAREER_CAP: "경력 상한 모름",
}
# 끝내 못 풀면 확인 필요로 가는 조건. 나머지(`off_target`)는 전송에서 빠진다
BLOCKING: tuple[str, ...] = (EMPTY_KEY, THIN_CONTENT, CAREER_CAP)
# 공고 하나를 다시 분류하는 횟수의 상한
MAX_RECHECKS = 2

# 1번 조건의 칸
KEY_FIELDS: tuple[str, ...] = (
    "job_role",
    "recruitment_end_at",
    "employment_type",
    "experience_type",
)
# 공고 내용 여덟 칸. 오공고 본문 칸과 같다 (`app/deliver/spring.py` 의 `_CONTENT_FIELDS`)
CONTENT_FIELDS: tuple[str, ...] = (
    "company_and_team_introduction",
    "responsibilities",
    "qualifications",
    "preferred_qualifications",
    "compensation",
    "benefits",
    "hiring_process",
    "recruitment_notice",
)
# 공고 내용이 이만큼 비면 2번 조건에 걸린다
THIN_CONTENT_EMPTY = 4
# 이 연차까지 지원할 수 있으면 4번 조건이 상한을 본다
JUNIOR_YEARS = 2

# 이미 보낸 공고. 확인 필요의 `SENT_SQL` 과 같은 조건이다
_SENT_SQL = (
    "EXISTS (SELECT 1 FROM spring_deliveries d"
    " WHERE d.source_url = n.source_url AND d.status = 'sent')"
)
# 다시 분류할 만큼 다 한 공고
_DONE_SQL = (
    "EXISTS (SELECT 1 FROM quality_gate_runs g WHERE g.raw_job_id = n.raw_job_id"
    f" AND g.attempts >= {MAX_RECHECKS})"
)


def _empty(name: str) -> str:
    return f"trim(coalesce(n.{name}, '')) = ''"


def _blocking_sqls() -> dict[str, str]:
    """확인 필요로 가는 세 조건. 설정을 읽지 않아 연결 없이 만든다."""
    empties = " + ".join(f"({_empty(name)})" for name in CONTENT_FIELDS)
    return {
        EMPTY_KEY: "(" + " OR ".join(_empty(name) for name in KEY_FIELDS) + ")",
        THIN_CONTENT: f"({empties}) >= {THIN_CONTENT_EMPTY}",
        CAREER_CAP: (
            "(n.experience_type IN ('EXPERIENCED', 'BOTH')"
            f" AND ({_empty('experience_min_years')}"
            f" OR CAST(n.experience_min_years AS INTEGER) <= {JUNIOR_YEARS})"
            f" AND {_empty('experience_max_years')})"
        ),
    }


def blocked_sql() -> str:
    """자동 전송을 막는 조건. `normalized_jobs n` 한 행을 본다. 전송과 확인 필요가 같이 쓴다."""
    return "(" + " OR ".join(_blocking_sqls().values()) + ")"


def reason_sqls(conn: sqlite3.Connection) -> dict[str, str]:
    """조건마다 `normalized_jobs n` 한 행을 보는 SQL 조각. 바인딩이 없다.

    대상 직군은 공고 목록의 직군 칸에서 바뀌므로 부를 때마다 읽는다. 직군 이름은 오공고 직군
    목록에 든 것만 저장되지만 따옴표는 겹쳐 둔다.
    """
    fields = target_fields.read_fields(conn)
    listed = ", ".join("'" + name.replace("'", "''") + "'" for name in fields)
    sqls = _blocking_sqls()
    return {
        EMPTY_KEY: sqls[EMPTY_KEY],
        THIN_CONTENT: sqls[THIN_CONTENT],
        OFF_TARGET: f"coalesce(n.job_field, '') NOT IN ({listed})",
        CAREER_CAP: sqls[CAREER_CAP],
    }


def reasons_of(
    conn: sqlite3.Connection, raw_job_ids: Sequence[int], now: str, *, fresh_only: bool = False
) -> dict[int, list[str]]:
    """공고마다 걸린 조건. 보내지 않았고 마감 전인 공고만 본다. 걸린 것이 없는 공고는 빠진다.

    직무마다 나뉜 공고는 한 번호라도 걸리면 그 공고의 조건이다 — 다시 분류는 공고 한 건 단위다.
    `fresh_only` 면 다시 분류를 `MAX_RECHECKS` 번 한 공고도 뺀다.
    """
    if not raw_job_ids:
        return {}
    # 전송 쪽이 이 모듈을 읽는다
    from app.deliver.spring import OPEN_SQL

    sqls = reason_sqls(conn)
    done = f" AND NOT {_DONE_SQL}" if fresh_only else ""
    columns = ", ".join(f"max({sql}) AS {name}" for name, sql in sqls.items())
    found: dict[int, list[str]] = {}
    ids = list(dict.fromkeys(raw_job_ids))
    # SQLite 의 바인딩 상한 아래로 나눈다
    for start in range(0, len(ids), 500):
        chunk = ids[start : start + 500]
        marks = ", ".join("?" for _ in chunk)
        rows = conn.execute(
            f"SELECT n.raw_job_id AS raw_job_id, {columns} FROM normalized_jobs n"
            f" WHERE n.raw_job_id IN ({marks}) AND NOT {_SENT_SQL} AND {OPEN_SQL}{done}"
            " GROUP BY n.raw_job_id",
            (*chunk, now),
        ).fetchall()
        for row in rows:
            hit = [name for name in sqls if row[name]]
            if hit:
                found[int(row["raw_job_id"])] = hit
    return found


def pick(conn: sqlite3.Connection, raw_job_ids: Sequence[int], now: str) -> dict[int, list[str]]:
    """다시 분류할 공고와 걸린 조건. 다시 분류를 두 번 한 공고는 빠진다."""
    return reasons_of(conn, raw_job_ids, now, fresh_only=True)


def backlog(conn: sqlite3.Connection, now: str) -> dict[int, list[str]]:
    """지금 쌓인 공고 중 다시 분류할 것 전부. 처음 켤 때 밀린 공고를 한 번 돌릴 때 쓴다.

    분류가 아직 안 된 공고는 빠진다 — 분류가 돌면 그 끝에서 검사가 본다.
    """
    rows = conn.execute(
        "SELECT DISTINCT n.raw_job_id FROM normalized_jobs n"
        " WHERE EXISTS (SELECT 1 FROM job_classifications c WHERE c.raw_job_id = n.raw_job_id)"
        f" AND NOT {_DONE_SQL} ORDER BY n.raw_job_id"
    ).fetchall()
    return pick(conn, [int(row[0]) for row in rows], now)


def record(conn: sqlite3.Connection, picked: dict[int, list[str]]) -> None:
    """다시 분류하기 전에 한 번을 센다. 도중에 죽어도 상한보다 더 돌지 않는다.

    처음 걸린 조건(`reasons`)은 첫 재분류 때만 적는다.
    """
    conn.executemany(
        "INSERT INTO quality_gate_runs (raw_job_id, reasons, attempts) VALUES (?, ?, 1)"
        " ON CONFLICT (raw_job_id) DO UPDATE SET attempts = attempts + 1",
        [(raw_job_id, ",".join(hit)) for raw_job_id, hit in picked.items()],
    )


def finish(conn: sqlite3.Connection, raw_job_ids: Sequence[int], now: str) -> None:
    """다시 분류한 공고에 그 뒤에도 걸린 조건을 적는다. 다 풀렸으면 빈 글자다."""
    after = reasons_of(conn, raw_job_ids, now)
    conn.executemany(
        "UPDATE quality_gate_runs SET reasons_after = ?, finished_at = datetime('now')"
        " WHERE raw_job_id = ?",
        [(",".join(after.get(raw_job_id, [])), raw_job_id) for raw_job_id in raw_job_ids],
    )
