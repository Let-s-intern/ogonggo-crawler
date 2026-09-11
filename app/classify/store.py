"""분류 결과를 읽고 쓴다. `job_classifications` 와 `job_field_suggestions` 둘을 건드린다.

`raw_jobs` 는 읽기만 한다 (`.claude/rules/data-safety.md`). 분류는 본문을 읽어 만든 값이라
출처를 고칠 이유가 없고, 고치면 분류가 틀렸을 때 되돌릴 원본이 사라진다.

`normalized_jobs` 에도 쓰지 않는다. 그 표를 갱신하는 것은 정규화 경로 하나뿐이고, 분류 결과는
정규화가 규칙 다음에 덮어 읽는다 (`app/normalize/engine.py`).

분류하지 못한 공고는 행이 없다. 빈 행을 넣으면 "분류했는데 아무것도 안 나왔다" 와 "아직
분류하지 않았다" 가 같은 모양이 되고, 다음 실행이 어느 쪽을 다시 돌아야 할지 모른다.

## 채우기와 제안이 가는 곳이 다르다

한 번의 호출 응답이 두 갈래로 나간다 (`.claude/tasks/todo/prd-side-workflows.md` 6절).
비어 있던 칸을 채운 아홉 칸은 지금까지처럼 `save_classification` 이 `job_classifications` 에
쓰고, 값이 있는 칸(`company`·`deadline`·`start_date`)에 원문이 다른 값을 낸 것은
`save_suggestions` 가 `job_field_suggestions` 에 쓴다. 이 표는 `app/normalize/engine.py` 의
어느 경로도 읽지 않는다 — 사람이 검수 화면에서 수락해야 `job_field_overrides` 로 옮겨 간다.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Any, Final

from app.classify.schema import COLLECTED_REVIEW_FIELDS, STORED_CLASSIFY_FIELDS

# `raw_jobs.raw_data_json` 에서 원문·본문·제목을 꺼내는 자리. JSON 함수는 SQLite 3.38+ 에 있다
_BODY = "json_extract(r.raw_data_json, '$.body')"
_TITLE = "json_extract(r.raw_data_json, '$.title')"
_SOURCE_TEXT = "json_extract(r.raw_data_json, '$.source_text')"

# 분류에 보내는 글. **원문이 있으면 원문이고, 없으면 본문으로 떨어진다.**
#
# 폴백이 필수다. 원문은 2026-08-28 부터 수집한 건에만 있고, 그 전에 쌓인 건에는 키가 아예
# 없다. 원문만 대상으로 삼으면 그 공고들이 분류에서 통째로 사라진다
# (`.claude/tasks/todo/prd-side-workflows.md` 4절).
#
# 상세가 API 인 사이트는 응답 전체를 편 원문이 붙는다 (`app/crawler/api_source.py`). 그것을
# 붙이기 전에 모은 건은 키가 없어 본문으로 돈다
_CLASSIFY_TEXT = f"coalesce(nullif({_SOURCE_TEXT}, ''), {_BODY}, '')"


# 분류 대상 범위 넷. 이 이름이 `side_workflows.target_scope` 에 그대로 저장되고,
# 종류마다 어느 값을 받는지는 `app/side/store.py` 가 본다.
#
# 이름을 조회하는 쪽에 두는 것은 범위가 무엇을 뜻하는지 아는 곳이 그 조회를 만드는 여기이기
# 때문이다. 두 벌을 두면 범위를 하나 더할 때 한쪽만 넓어지고, 저장은 되는데 아무것도 고르지
# 못하는 워크플로우가 생긴다
UNCLASSIFIED: Final = "unclassified"
EMPTY_FIELDS: Final = "empty_fields"
RECENT: Final = "recent"
ALL: Final = "all"

# 분류 워크플로우가 받는 범위 전부. `app/side/store.py` 의 `SCOPES[classify]` 가 이 값이다
CLASSIFY_SCOPES: tuple[str, ...] = (UNCLASSIFIED, EMPTY_FIELDS, RECENT, ALL)


# 분류 행의 칸이 전부 비었는지 보는 조건. 칸 목록은 `app/classify/schema.py` 가 정한다 —
# 0016 이 셋을 지웠고 0017 이 하나를 더했다. 칸 이름이나 개수를 여기 적어 두면 다음에 칸이
# 늘거나 줄 때 이 조회만 옛 목록으로 남는다
_ALL_FIELDS_EMPTY = " AND ".join(f"coalesce(c.{name}, '') = ''" for name in STORED_CLASSIFY_FIELDS)


class ClassifyScopeError(ValueError):
    """분류가 도는 대상 범위로 쓸 수 없는 값. 부르는 쪽이 사유를 그대로 옮긴다."""


def _scope_from(scope: str, days: int | None) -> tuple[str, tuple[Any, ...]]:
    """범위 하나의 `FROM ... WHERE ...` 와 그 파라미터.

    목록과 건수가 이 문장 하나를 같이 쓴다. 갈리면 확인 창이 적은 대상 건수와 실제로 도는
    건수가 다른데, 그때 어느 쪽이 맞는지 알 방법이 없다 (PRD 2절).

    **네 범위 모두 "보낼 글이 있다" 를 먼저 요구한다.** 원문도 본문도 없는 공고는 부를 이유가
    없는 호출이고, 그 호출은 `empty_body` 로 끝나 실패 건수만 부풀린다. 그 조건은
    `_CLASSIFY_TEXT` 하나가 정한다 — 범위마다 다시 쓰면 범위마다 대상이 갈린다.
    """
    if scope not in CLASSIFY_SCOPES:
        raise ClassifyScopeError(
            f"분류가 도는 대상 범위가 아니다: {scope!r}. {', '.join(CLASSIFY_SCOPES)} 중 하나다"
        )
    if scope == UNCLASSIFIED:
        # 아직 분류 행이 없는 건. 지금 도는 조회 그대로다 (`pending_ids`)
        return (
            f"""
              FROM raw_jobs r
              LEFT JOIN job_classifications c ON c.raw_job_id = r.id
             WHERE c.raw_job_id IS NULL
               AND {_CLASSIFY_TEXT} <> ''
            """,
            (),
        )
    if scope == EMPTY_FIELDS:
        # 분류 행은 있는데 칸이 전부 빈 건. **행이 없는 건과 다르다** — 이쪽은 이미 한 번
        # 돌았고 아무것도 나오지 않은 것이라, 프롬프트나 모델을 바꾼 뒤에 다시 돌릴 대상이다
        return (
            f"""
              FROM raw_jobs r
              JOIN job_classifications c ON c.raw_job_id = r.id
             WHERE {_CLASSIFY_TEXT} <> ''
               AND {_ALL_FIELDS_EMPTY}
            """,
            (),
        )
    if scope == RECENT:
        if days is None or days < 1:
            raise ClassifyScopeError(
                f"범위가 {RECENT} 면 최근 며칠을 볼지 1 이상으로 받아야 한다: {days}"
            )
        # **분류 여부를 보지 않는다.** 다시 분류하는 범위라 이미 분류된 건도 들어가고, 그
        # 결과는 같은 행을 덮는다. 사람이 넣은 `job_field_overrides` 는 정규화가 분류 다음에
        # 덮으므로 그대로 살아 있다 (PRD 2절)
        #
        # 기준은 `raw_jobs.crawled_at` 이다. 공고에 적힌 게시일이 아니라 우리가 언제 가져왔나
        # 이고, 그것이 "최근에 들어온 것을 다시 돌린다" 가 묻는 것이다
        return (
            f"""
              FROM raw_jobs r
             WHERE {_CLASSIFY_TEXT} <> ''
               AND r.crawled_at >= datetime('now', ?)
            """,
            (f"-{days} days",),
        )
    if scope == ALL:
        # 보낼 글이 있는 건 전부. 이미 분류된 것도, 언제 수집했든 들어간다.
        #
        # **전량 재지출이다.** 640건이면 약 285만 토큰이고 돌기 시작하면 멈출 수 없어서,
        # 이 범위를 저장할 때는 화면이 대상 건수를 확인 창에 적는다 (PRD 2절). 그 숫자가
        # `scope_count` 이고 여기서 세는 것과 같은 문장이다
        return (f"FROM raw_jobs r WHERE {_CLASSIFY_TEXT} <> ''", ())
    # 위 넷이 `CLASSIFY_SCOPES` 전부다. 여기 닿았다면 범위를 더하고 조회를 만들지 않은 것이다
    raise ClassifyScopeError(f"조회가 없는 범위다: {scope!r}")


def scope_ids(
    conn: sqlite3.Connection, scope: str, *, days: int | None = None, limit: int | None = None
) -> list[int]:
    """그 범위의 공고 id. **최근 수집한 것부터다.** 읽기 전용이다.

    순서는 네 범위가 같다. 상한에 걸려 잘려 나가는 쪽이 언제나 오래된 것이어야 하고, 범위마다
    순서가 다르면 상한을 걸었을 때 무엇이 도는지 화면이 설명할 수 없다 (2026-08-27 결정).
    """
    body, params = _scope_from(scope, days)
    bound = "" if limit is None else " LIMIT ?"
    rows = conn.execute(
        f"SELECT r.id AS id {body} ORDER BY r.id DESC{bound}",
        params if limit is None else (*params, limit),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def scope_count(conn: sqlite3.Connection, scope: str, *, days: int | None = None) -> int:
    """그 범위의 대상 건수. 화면의 확인 창이 이 숫자를 적는다. 읽기 전용이다.

    `scope_ids` 와 **같은 문장**을 쓴다. 세는 조회를 따로 쓰면 확인 창이 적은 건수와 실제로
    도는 건수가 갈리고, 그때 어느 쪽이 맞는지 알 방법이 없다. `all` 에서는 그 차이가 그대로
    토큰 값이다 (PRD 2절).

    1회 상한(`side_workflows.batch_limit`)은 여기 없다. 확인 창이 묻는 것은 "이 범위가 몇
    건이냐" 이고, 그중 몇 건씩 끊어 도는지는 실행이 정한다.
    """
    body, params = _scope_from(scope, days)
    row = conn.execute(f"SELECT count(*) AS n {body}", params).fetchone()
    return int(row["n"])


def pending_ids(conn: sqlite3.Connection, limit: int | None = None) -> list[int]:
    """보낼 글이 있고 아직 분류되지 않은 공고. **최근 수집한 것부터다.** 읽기 전용이다.

    조건은 "원문이나 본문이 있다" 이다. 본문 하나만 보면, 원문은 뽑았는데 본문이 빈 건이
    대상에서 조용히 빠진다 — 그 건은 보낼 글이 있는데도 영영 분류되지 않는다.

    `limit` 은 한 번에 도는 건수의 상한이다. 640건을 한 번에 돌리면 멈출 수가 없다
    (`.claude/tasks/memos/보류/llm-classify/prd-llm-classify.md`).

    2026-08-27 에 오래된 것부터에서 최근 것부터로 뒤집었다. 크레딧이 끊겨 313건이 밀려 있는데
    오래된 것부터 돌면 **오늘 들어온 공고가 맨 뒤에 선다.** 소비 측이 지금 필요한 것은 오늘
    올라온 공고이고, 밀린 것은 급하지 않다. 신규가 하루 0~1건이라 이 순서로도 밀린 것은
    결국 다 돈다.
    """
    return scope_ids(conn, UNCLASSIFIED, limit=limit)


def pending_count(conn: sqlite3.Connection) -> int:
    """아직 분류되지 않은 공고 수. 화면이 "몇 건 남았나" 로 읽는다.

    `pending_ids` 와 같은 조건이어야 한다. 갈리면 화면의 남은 건수와 실제로 도는 건수가
    다르다.
    """
    return scope_count(conn, UNCLASSIFIED)


def read_source(conn: sqlite3.Connection, raw_job_id: int) -> str:
    """분류에 보낼 글. 원문이 있으면 원문이고 없으면 본문이다. 읽기 전용이다.

    본문만 읽던 자리다. 원문은 본문에 더해 그 공고의 이름표 값(회사·마감·근무지·경력)을
    담고 있어, 본문만 보내면 그 값들이 어느 칸에도 들어가지 못했다
    (`.claude/site-recipes/source-text-container.md`).

    둘 다 없으면 빈 문자열이고, 부르는 쪽이 그것을 `empty_body` 로 끝낸다.
    """
    row = conn.execute(
        f"SELECT {_CLASSIFY_TEXT} AS source FROM raw_jobs r WHERE r.id = ?",
        (raw_job_id,),
    ).fetchone()
    return "" if row is None else str(row["source"])


def read_title(conn: sqlite3.Connection, raw_job_id: int) -> str:
    """그 공고의 제목. 없으면 빈 문자열이다. 읽기 전용이다.

    `job_role` 이 여기서 온다. 본문에 없고 제목에만 있는 값이라 본문만 보내면 그 칸은 영원히
    빈다 (`tests/test_job_role_source.py`).
    """
    row = conn.execute(
        f"SELECT coalesce({_TITLE}, '') AS title FROM raw_jobs r WHERE r.id = ?",
        (raw_job_id,),
    ).fetchone()
    return "" if row is None else str(row["title"])


# `COLLECTED_REVIEW_FIELDS` 셋을 `raw_data_json` 에서 꺼내는 자리. 이름이 그 칸 이름과 같다
_REVIEW_EXTRACTORS = {
    name: f"json_extract(r.raw_data_json, '$.{name}')" for name in COLLECTED_REVIEW_FIELDS
}


def read_current_values(conn: sqlite3.Connection, raw_job_id: int) -> dict[str, str]:
    """`company`·`deadline`·`start_date` 중 수집이 이미 채운 값. 읽기 전용이다.

    무엇이 이미 채워져 있는지 모르면 분류가 "원문과 다르다" 를 말할 수 없다 — 프롬프트에
    이 값을 실어 보내는 것이 `app/classify/classifier.py` 의 `build_prompt` 다.

    빈 문자열인 칸은 결과에 없다. 값이 없다는 것은 비교할 것이 없다는 뜻이지, 빈 문자열과
    다른 값을 제안하라는 뜻이 아니다 — 이 셋은 채우기 대상이 아니라 있는 값을 검사하는
    대상이다 (`.claude/tasks/todo/prd-side-workflows.md` 6절).
    """
    columns = ", ".join(f"{sql} AS {name}" for name, sql in _REVIEW_EXTRACTORS.items())
    row = conn.execute(f"SELECT {columns} FROM raw_jobs r WHERE r.id = ?", (raw_job_id,)).fetchone()
    if row is None:
        return {}
    return {
        name: str(row[name])
        for name in COLLECTED_REVIEW_FIELDS
        if row[name] is not None and str(row[name]).strip()
    }


def read_classification(conn: sqlite3.Connection, raw_job_id: int) -> dict[str, str]:
    """그 공고의 분류 결과. 아직 분류되지 않았으면 빈 dict 다. 읽기 전용이다.

    빈 dict 와 "전부 빈 문자열인 dict" 는 뜻이 다르다. 앞은 아직 돌지 않은 것이고 뒤는
    돌았는데 본문이 아무것도 주지 않은 것이다.
    """
    row = conn.execute(
        f"SELECT {', '.join(STORED_CLASSIFY_FIELDS)} FROM job_classifications WHERE raw_job_id = ?",
        (raw_job_id,),
    ).fetchone()
    if row is None:
        return {}
    return {name: str(row[name] or "") for name in STORED_CLASSIFY_FIELDS}


def read_evidence(conn: sqlite3.Connection, raw_job_id: int) -> dict[str, str]:
    """판정 칸의 근거 문장. 아직 분류되지 않았거나 판정이 없으면 빈 dict 다. 읽기 전용이다."""
    row = conn.execute(
        "SELECT evidence_json FROM job_classifications WHERE raw_job_id = ?", (raw_job_id,)
    ).fetchone()
    if row is None:
        return {}
    try:
        parsed = json.loads(row["evidence_json"] or "{}")
    except json.JSONDecodeError:
        return {}
    return {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}


def save_classification(
    conn: sqlite3.Connection,
    raw_job_id: int,
    fields: Mapping[str, str],
    *,
    model: str,
    dropped: Sequence[str] = (),
    evidence: Mapping[str, str] | None = None,
) -> None:
    """분류 결과를 넣거나 덮는다. 빈 값은 NULL 로 들어간다.

    덮는 것이 맞다. 분류는 본문에서 다시 만들 수 있는 값이라 이력을 쌓을 이유가 없고,
    한 공고에 결과가 둘이면 어느 쪽이 지금 값인지 알 수 없다.

    `evidence` 는 판정 칸을 그렇게 고른 근거 문장이다. 남기지 않으면 나중에 "이 공고가 왜
    경력으로 분류됐나" 에 답할 수 없다 (`migrations/0015_classification_evidence.sql`).
    """
    columns = (*STORED_CLASSIFY_FIELDS, "dropped_fields", "model", "evidence_json")
    values = [fields.get(name, "").strip() or None for name in STORED_CLASSIFY_FIELDS]
    values.append(", ".join(dropped))
    values.append(model)
    values.append(json.dumps(dict(evidence or {}), ensure_ascii=False))
    assignments = ", ".join(f"{name} = excluded.{name}" for name in columns)
    conn.execute(
        f"""
        INSERT INTO job_classifications (raw_job_id, {", ".join(columns)})
        VALUES ({", ".join("?" for _ in range(len(columns) + 1))})
        ON CONFLICT (raw_job_id) DO UPDATE
           SET {assignments}, classified_at = datetime('now')
        """,
        (raw_job_id, *values),
    )


def save_suggestions(
    conn: sqlite3.Connection,
    raw_job_id: int,
    suggestions: Mapping[str, str],
    reasons: Mapping[str, str] | None = None,
) -> None:
    """값이 있는 칸에 원문이 다르다고 낸 값을 `job_field_suggestions` 에 넣거나 덮는다.

    `suggestions` 에 없는 칸은 건드리지 않는다 — 이번 호출이 그 칸을 다시 말하지 않았다고
    해서 옛 제안이 틀렸다고 볼 근거는 없다. 사람이 검수 화면에서 수락하거나 거절해야 그 행이
    사라진다.

    같은 칸에 제안이 둘이면 어느 것을 보고 있는지 알 수 없어(`(raw_job_id, field_name)`
    UNIQUE), 새 제안이 옛 제안을 덮는다.
    """
    reasons = reasons or {}
    for field_name, value in suggestions.items():
        if not value.strip():
            continue
        conn.execute(
            """
            INSERT INTO job_field_suggestions (raw_job_id, field_name, value, reason)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (raw_job_id, field_name) DO UPDATE
               SET value = excluded.value, reason = excluded.reason,
                   created_at = datetime('now')
            """,
            (raw_job_id, field_name, value, reasons.get(field_name, "").strip()),
        )


def read_suggestions(conn: sqlite3.Connection, raw_job_id: int) -> dict[str, dict[str, str]]:
    """그 공고에 남아 있는 제안. 필드명이 키고, 값은 `value`·`reason` 이다. 읽기 전용이다.

    검수 화면(11.6)이 "제안 있음" 을 보이는 자리다. 수락은 `job_field_overrides` 에 넣는
    일이고, 거절은 이 표의 행만 지우는 일이다 — 둘 다 이 파일 밖(`app/api/review.py`)의
    일이라 여기는 읽기만 한다.
    """
    rows = conn.execute(
        "SELECT field_name, value, reason FROM job_field_suggestions WHERE raw_job_id = ?",
        (raw_job_id,),
    ).fetchall()
    return {
        str(row["field_name"]): {"value": str(row["value"]), "reason": str(row["reason"] or "")}
        for row in rows
    }


def read_suggestions_batch(
    conn: sqlite3.Connection, raw_job_ids: Sequence[int]
) -> dict[int, dict[str, dict[str, str]]]:
    """`read_suggestions` 의 여러 건 버전. 검수 표 한 페이지가 이것으로 N+1 조회를 피한다.

    행마다 `read_suggestions` 를 부르면 한 페이지(최대 100건)에 조회가 100번 붙는다. 표는
    "이 칸에 제안이 있다" 는 배지만 필요하지만, 모달과 같은 모양(값·이유)으로 돌려주는 것이
    두 자리가 서로 다른 것을 반환하는 것보다 낫다 — 호출하는 쪽이 값을 쓰지 않으면 그만이다.
    """
    if not raw_job_ids:
        return {}
    marks = ",".join("?" for _ in raw_job_ids)
    rows = conn.execute(
        "SELECT raw_job_id, field_name, value, reason FROM job_field_suggestions"
        f" WHERE raw_job_id IN ({marks})",
        list(raw_job_ids),
    ).fetchall()
    found: dict[int, dict[str, dict[str, str]]] = {}
    for row in rows:
        found.setdefault(int(row["raw_job_id"]), {})[str(row["field_name"])] = {
            "value": str(row["value"]),
            "reason": str(row["reason"] or ""),
        }
    return found
