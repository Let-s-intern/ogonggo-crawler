"""제공 API. 채용공고 사이트(별도 서비스)가 정규화된 데이터를 가져가는 경계면이다.

응답의 필드·타입·모양은 `.claude/docs/api-contract.md` 가 정한다. 이 파일과 그 문서는 같은
커밋에서 바뀐다 — 어긋나면 소비 측이 조용히 데이터를 못 받는다.

커서 기반이다. 오프셋으로 만들면 폴링 사이에 삽입된 행이 앞 페이지를 밀어내서, 소비 측이
받지 못한 채 지나가는 건이 생긴다. 커서는 `(normalized_at, id)` 한 쌍이고, 정렬도 같은 쌍의
오름차순이다. `normalized_at` 은 초 단위라 같은 값이 여러 행에 걸리므로, `id` 없이는 경계에서
행이 잘리거나 겹친다.

`normalized_at` 의 의미는 건드리지 않는다. 소비 측 폴링 커서가 이 값에 걸려 있다. 저장 형식은
SQLite `datetime('now')` 그대로 두고, 계약이 요구하는 ISO8601 모양은 응답에서만 만든다.
"""

from __future__ import annotations

import base64
import binascii
import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app import db

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

# 계약이 정한 한 번에 가져갈 건수
DEFAULT_LIMIT = 100
MAX_LIMIT = 500
# `normalized_at` 의 저장 형식. SQLite `datetime('now')` 가 만드는 UTC 초 단위 문자열이다
STORED_FORMAT = "%Y-%m-%d %H:%M:%S"

_SELECT = """
    SELECT id, parent_company_name, company_name, title, job_field, job_role,
           recruitment_end_at, body, qualifications,
           recruitment_start_at, employment_type, experience_type, region,
           responsibilities, preferred_qualifications, hiring_process, recruitment_notice,
           company_and_team_introduction, compensation, benefits, education_level,
           recruitment_headcount,
           source_url, normalized_at
      FROM normalized_jobs
"""


class JobOut(BaseModel):
    """계약의 `items` 한 건. 필드 이름과 순서는 문서를 그대로 따른다.

    0031 이 칸 이름을 오공고(Spring) `Job` 엔티티의 칼럼 이름에 맞췄다. 제목에서 옮기던 자유
    텍스트 직무는 오공고에 받을 칸이 없어 뺐고, 그 이름(`job_role`)은 직무 분류의 소분류가 받았다
    (`migrations/0031_spring_field_names.sql`).

    0018 이 회사명을 두 칸으로 갈랐다. `parent_company_name` 는 그 채용 사이트를 운영하는 기업이고
    거의 언제나 값이 있다. `company_name` 는 그 공고가 말한 계열사이고, 계열사를 말하지 않는
    사이트에서는 `null` 이다 — 그 자리를 모회사 이름으로 메우지 않는다
    (`migrations/0018_parent_company.sql`).

    `job_field`(직군)/`job_role`(직무)은 분류가 `job_taxonomy`(운영자가 어드민에서 바꾸는 표)의
    대분류·소분류에서 골라 채우는 닫힌 값이다. 아직 분류를 돌리지 않았거나 본문으로 판단이 갈리지
    않으면 `null` 이다 (`.claude/docs/api-contract.md`).

    0028 이 오공고가 받는 다섯 칸을 더했다 — 회사·팀 소개, 급여·처우, 복지·혜택, 학력,
    모집인원. 모집인원은 적힌 그대로의 글자이고 숫자가 아니다
    (`migrations/0028_add_posting_detail_fields.sql`).

    사이트가 그 값을 주지 않으면 `null` 이다. 없는 값을 다른 값으로 채우지 않는다.
    """

    id: int
    parent_company_name: str | None
    company_name: str | None
    title: str | None
    job_field: str | None
    job_role: str | None
    recruitment_end_at: str | None
    body: str | None
    qualifications: str | None
    # 모집 시작일. `recruitment_end_at`(모집 마감일)의 짝이고 그 필드를 대신하지 않는다
    recruitment_start_at: str | None
    employment_type: str | None
    experience_type: str | None
    region: str | None
    responsibilities: str | None
    preferred_qualifications: str | None
    hiring_process: str | None
    recruitment_notice: str | None
    company_and_team_introduction: str | None
    compensation: str | None
    benefits: str | None
    education_level: str | None
    recruitment_headcount: str | None
    source_url: str
    normalized_at: str


class JobPage(BaseModel):
    items: list[JobOut]
    next_cursor: str | None
    has_more: bool


class DeliveredRequest(BaseModel):
    ids: list[int]


class DeliveredOut(BaseModel):
    """무엇이 실제로 찍혔는지. 계약은 응답 모양을 정하지 않아 진단에 필요한 것만 담는다."""

    marked: int
    already_delivered: int
    missing: list[int]


def get_connection() -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def encode_cursor(normalized_at: str, job_id: int) -> str:
    """커서는 소비 측이 열어 보지 않는 값이다. 그대로 돌려주기만 하면 된다."""
    payload = json.dumps({"t": normalized_at, "i": job_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> tuple[str, int]:
    """망가진 커서는 400 이다. 조용히 처음부터 주면 소비 측이 같은 데이터를 다시 받는다."""
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        return str(payload["t"]), int(payload["i"])
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"reason": "invalid_cursor", "message": "cursor 를 읽을 수 없다"},
        ) from exc


def parse_updated_after(value: str) -> str:
    """ISO8601 을 저장 형식으로 옮긴다. 비교는 문자열로 한다 — 형식이 고정폭이라 순서가 같다.

    타임존이 없으면 UTC 로 본다. 저장된 값이 UTC 라, 로컬 시각으로 해석하면 소비 측이 시차만큼
    받지 못한 구간이 생긴다.
    """
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "invalid_updated_after",
                "message": f"ISO8601 시각이 아니다: {value}",
            },
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime(STORED_FORMAT)


def clamp_limit(value: int) -> int:
    """계약의 상한 500 을 넘는 요청은 거절이 아니라 절삭이다. 커서가 있어 나머지는 이어 받는다."""
    return max(1, min(value, MAX_LIMIT))


def _iso(stored: str) -> str:
    """저장 형식(`YYYY-MM-DD HH:MM:SS`, UTC)을 계약의 ISO8601 모양으로 옮긴다."""
    text = stored.strip().replace(" ", "T")
    if text.endswith("Z") or "+" in text:
        return text
    return text + "Z"


def _out(row: sqlite3.Row) -> JobOut:
    return JobOut(
        id=int(row["id"]),
        parent_company_name=row["parent_company_name"],
        company_name=row["company_name"],
        title=row["title"],
        job_field=row["job_field"],
        job_role=row["job_role"],
        recruitment_end_at=row["recruitment_end_at"],
        body=row["body"],
        qualifications=row["qualifications"],
        recruitment_start_at=row["recruitment_start_at"],
        employment_type=row["employment_type"],
        experience_type=row["experience_type"],
        region=row["region"],
        responsibilities=row["responsibilities"],
        preferred_qualifications=row["preferred_qualifications"],
        hiring_process=row["hiring_process"],
        recruitment_notice=row["recruitment_notice"],
        company_and_team_introduction=row["company_and_team_introduction"],
        compensation=row["compensation"],
        benefits=row["benefits"],
        education_level=row["education_level"],
        recruitment_headcount=row["recruitment_headcount"],
        source_url=str(row["source_url"]),
        normalized_at=_iso(str(row["normalized_at"])),
    )


@router.get("", response_model=JobPage)
def list_jobs(
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    updated_after: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query()] = DEFAULT_LIMIT,
    cursor: Annotated[str | None, Query()] = None,
) -> JobPage:
    """정규화된 공고를 `normalized_at` 오름차순으로 준다.

    `updated_after` 는 `normalized_at` 기준이고 그 시각을 포함하지 않는다. 소비 측이 마지막으로
    받은 시각을 그대로 넣으면 같은 건이 다시 오지 않는다.

    `next_cursor` 는 항목이 있으면 항상 돌려준다. 마지막 페이지에서 비워 보내면 소비 측이 읽은
    위치를 잃고, 다음 폴링에서 처음부터 다시 받는다. 더 받을 것이 있는지는 `has_more` 가 말한다.
    """
    size = clamp_limit(limit)
    clauses: list[str] = []
    params: list[Any] = []
    if updated_after is not None:
        clauses.append("normalized_at > ?")
        params.append(parse_updated_after(updated_after))
    if cursor is not None:
        last_at, last_id = decode_cursor(cursor)
        clauses.append("(normalized_at > ? OR (normalized_at = ? AND id > ?))")
        params.extend([last_at, last_at, last_id])

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    # 한 건 더 읽어 다음 페이지가 있는지를 별도 count 없이 판정한다
    rows = conn.execute(
        f"{_SELECT}{where} ORDER BY normalized_at, id LIMIT ?",
        [*params, size + 1],
    ).fetchall()

    has_more = len(rows) > size
    page = rows[:size]
    next_cursor: str | None = cursor
    if page:
        last = page[-1]
        next_cursor = encode_cursor(str(last["normalized_at"]), int(last["id"]))
    return JobPage(items=[_out(row) for row in page], next_cursor=next_cursor, has_more=has_more)


@router.post("/delivered", response_model=DeliveredOut)
def mark_delivered(
    payload: DeliveredRequest,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> DeliveredOut:
    """소비 측이 가져간 건에 `delivered_at` 을 찍는다.

    **이 경로만 `delivered_at` 을 쓴다** (`.claude/rules/data-safety.md`). 크롤링·재정규화·수동
    수정은 이 컬럼을 건드리지 않는다.

    이미 찍힌 건은 덮어쓰지 않는다. 시각을 다시 쓰면 "언제 넘어갔는가" 가 마지막 폴링 시각으로
    밀려서, 재전송 여부를 이 값으로 판단할 수 없게 된다. `WHERE delivered_at IS NULL` 이
    그것을 막는다.

    시각은 SQLite `datetime('now')` 로 찍는다. 다른 테이블의 시각과 형식이 같아야 한다.
    """
    unique = sorted(set(payload.ids))
    if not unique:
        return DeliveredOut(marked=0, already_delivered=0, missing=[])

    placeholders = ",".join("?" * len(unique))
    rows = conn.execute(
        f"SELECT id, delivered_at FROM normalized_jobs WHERE id IN ({placeholders})",
        unique,
    ).fetchall()
    found = {int(row["id"]): row["delivered_at"] for row in rows}
    already = sum(1 for value in found.values() if value is not None)

    cursor = conn.execute(
        f"""
        UPDATE normalized_jobs
           SET delivered_at = datetime('now')
         WHERE id IN ({placeholders}) AND delivered_at IS NULL
        """,
        unique,
    )
    return DeliveredOut(
        marked=int(cursor.rowcount),
        already_delivered=already,
        missing=[job_id for job_id in unique if job_id not in found],
    )
