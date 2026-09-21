"""정규화한 공고를 오공고(Spring) 관리자 API 로 등록한다 (2026-09-15 결정).

## 새 공고만 등록한다

보낸 공고가 나중에 바뀌는 일은 드물어 러프하게 처리한다. 아직 보내지 않은 공고만
`POST /api/v1/internal/jobs` 로 등록하고, 받은 공고 id 를 `spring_deliveries` 에 적는다
(`migrations/0036_spring_deliveries.sql`). 보낸 뒤 값이 바뀌어도 다시 보내지 않고, 크롤러에서 지운
공고도 오공고에서 지우지 않는다. 직무마다 나뉜 공고는 주소 끝에 `#번호` 가 붙어 새 공고로 등록된다.

이미 등록된 원문 주소라 409 가 오면 `GET ?sourceUrl=` 로 id 를 찾아 보낸 것으로 적는다 — id 를 적기
전에 크롤러가 죽었거나 DB 를 옮긴 경우다.

## 보내지 않는 공고

- 오공고가 반드시 받는 칸(회사명·제목·고용 형태·경력 구분·학력·모집 유형·원문 주소)이 빈 공고. 대개
  아직 분류하지 않은 공고이고, 분류가 끝나면 다음 전송에 들어간다
- **자동 전송은 더 깐깐하다** (2026-09-18 결정). 공고 화면의 기본 정보·모집 조건 칸이 다 차야 보낸다
  (`AUTO_FIELDS`). 최소 경력 연수와 모집 인원은 원문에 없는 일이 많아 보지 않는다. 모회사는 없는
  회사가 많아 보지 않고, 모집 마감은 기간 채용일 때만 본다 — 상시 채용에는 마감일이 없다. 칸이 빈
  공고는 사람이 채우거나 골라 보내기로 보낸다. 골라 보내기와 `지금 보내기` 는 필수 칸만 본다
- 아직 보내지 않았는데 마감 일시가 이미 지난 공고
- 세 번 실패한 공고. 마지막 거절 사유는 전달 화면에 남는다

공고 본문과 판정 근거는 보내지 않는다 — 오공고에 받는 칸이 없다.

## 시각

크롤러의 모집 일시는 수집한 사이트의 한국 시각 그대로이고, 오공고도 서울 시각 `LocalDateTime` 이다.
오프셋 없이 `YYYY-MM-DDTHH:MM:SS` 로 보낸다. 마감이 지났는지도 표시 시간대의 지금과 견준다.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app.classify.schema import VALUE_LABELS
from app.config import Settings, get_settings
from app.deliver import settings as store

logger = logging.getLogger(__name__)

JOBS_PATH = "/api/v1/internal/jobs"
API_KEY_HEADER = "X-Internal-Api-Key"
TIMEOUT_SECONDS = 20.0
# 이만큼 실패한 공고는 더 보내지 않는다. 같은 거절을 분류 때마다 되풀이하지 않는다
MAX_ATTEMPTS = 3

SENT = "sent"
FAILED = "failed"

# 오공고가 비어 있으면 받지 않는 칸
REQUIRED: tuple[str, ...] = (
    "companyName",
    "title",
    "employmentType",
    "experienceType",
    "educationLevel",
    "recruitmentType",
    "sourceUrl",
)

# 본문 칸. 크롤러 칸 이름과 오공고 요청 칸 이름의 짝이다
_CONTENT_FIELDS: tuple[tuple[str, str], ...] = (
    ("companyAndTeamIntroduction", "company_and_team_introduction"),
    ("responsibilities", "responsibilities"),
    ("qualifications", "qualifications"),
    ("preferredQualifications", "preferred_qualifications"),
    ("compensation", "compensation"),
    ("benefits", "benefits"),
    ("hiringProcess", "hiring_process"),
    ("recruitmentNotice", "recruitment_notice"),
)

_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
_INTEGER = re.compile(r"\d{1,9}")

# 테스트가 `httpx.MockTransport` 를 끼우는 자리. 운영에서는 None 이다
transport: httpx.AsyncBaseTransport | None = None

# 분류 끝의 자동 전송과 `지금 보내기` 가 겹치면 같은 공고를 두 번 등록하려 든다. 한 번에 하나만 돈다
_running = threading.Lock()


@dataclass
class DeliveryResult:
    """전송 한 번의 결과.

    `reason` 은 아예 보내지 않은 이유다(꺼짐, 주소·키 없음, 이미 보내는 중).
    """

    sent: int = 0
    failed: int = 0
    reason: str = ""
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Failure:
    source_url: str
    attempts: int
    last_error: str
    updated_at: str


@dataclass(frozen=True)
class Overview:
    """전달 화면에 보이는 숫자와 최근 실패."""

    sent: int
    failed: int
    failures: list[Failure]


def payload(job: Mapping[str, Any] | sqlite3.Row) -> dict[str, Any]:
    """정규화 행 하나를 오공고 등록 요청 본문으로 옮긴다. 빈 글자는 null 이다.

    회사명은 자회사가 있으면 자회사, 없으면 모회사다. 오공고는 `companyName` 을 실제 채용 주체로
    받고 모회사를 따로 받는다. 목록 밖 판정 값(옛 한글 값)은 null 로 보내 필수 칸 검사에서 걸리게
    한다.
    """
    company = _text(job["company_name"]) or _text(job["parent_company_name"])
    parent = _text(job["parent_company_name"])
    body: dict[str, Any] = {
        "companyName": company,
        "parentCompanyName": parent if parent and parent != company else None,
        "title": _text(job["title"]),
        "jobField": _text(job["job_field"]),
        "jobRole": _text(job["job_role"]),
        "industry": _text(job["industry"]),
        "coverImageUrl": _text(job["cover_image_url"]),
        "employmentType": _choice(job, "employment_type"),
        "experienceType": _choice(job, "experience_type"),
        "experienceMinYears": _integer(job["experience_min_years"]),
        "educationLevel": _choice(job, "education_level"),
        "region": _text(job["region"]),
        "recruitmentType": _choice(job, "recruitment_type"),
        "recruitmentHeadcount": _integer(job["recruitment_headcount"]),
        "recruitmentStartAt": _datetime(job["recruitment_start_at"]),
        "recruitmentEndAt": _datetime(job["recruitment_end_at"]),
        "closesWhenFilled": _boolean(job["closes_when_filled"]),
        "autoCloseEnabled": _boolean(job["auto_close_enabled"]),
        **{name: _text(job[column]) for name, column in _CONTENT_FIELDS},
        "applicationMethod": _choice(job, "application_method"),
        "applicationEmail": _text(job["application_email"]),
        "inquiryEmail": _text(job["inquiry_email"]),
        "sourceUrl": _text(job["source_url"]),
    }
    if body["recruitmentType"] == "ALWAYS_OPEN":
        # 오공고는 상시 채용에 종료 일시가 있으면 받지 않는다
        body["recruitmentEndAt"] = None
    return body


def missing(body: Mapping[str, Any]) -> list[str]:
    """등록 요청에서 빈 필수 칸."""
    return [name for name in REQUIRED if not body.get(name)]


# 아직 보내지 않았거나, 실패했지만 다시 보낼 차례가 남은 공고
_UNSENT = "(d.source_url IS NULL OR (d.status = 'failed' AND d.attempts < ?))"
# 마감 일시가 아직 지나지 않은 공고. 공고 목록의 `확인 필요` 도 같은 조건을 쓴다
OPEN_SQL = "(n.recruitment_end_at IS NULL OR n.recruitment_end_at >= ?)"
_OPEN = OPEN_SQL
# 오공고가 반드시 받는 칸이 SQL 로 보기에 차 있다. 목록 밖 값은 보내기 직전에 `missing` 이 거른다.
# 공고 목록의 `필수 칸 빔` 표시도 이 조건을 뒤집어 쓴다
READY_SQL = """(trim(coalesce(n.title, '')) != ''
           AND trim(coalesce(n.company_name, '') || coalesce(n.parent_company_name, '')) != ''
           AND n.employment_type IS NOT NULL
           AND n.experience_type IS NOT NULL
           AND n.education_level IS NOT NULL
           AND n.recruitment_type IS NOT NULL)"""
_READY = READY_SQL

# 자동 전송이 차 있기를 바라는 칸. 공고 화면(`app/api/job_detail.py`)의 기본 정보·모집 조건에서
# 최소 경력 연수·모집 인원·모회사를 뺀 것이다. 회사는 자회사나 모회사 중 하나, 모집 마감은 기간
# 채용만 본다
AUTO_FIELDS: tuple[str, ...] = (
    "company_name",
    "title",
    "industry",
    "job_field",
    "job_role",
    "cover_image_url",
    "employment_type",
    "experience_type",
    "education_level",
    "region",
    "recruitment_type",
    "application_method",
    "recruitment_start_at",
    "recruitment_end_at",
    "closes_when_filled",
    "auto_close_enabled",
)
_COMPANY = "company_name"
_END = "recruitment_end_at"


def _filled_sql(name: str) -> str:
    if name == _COMPANY:
        return "trim(coalesce(n.company_name, '') || coalesce(n.parent_company_name, '')) != ''"
    if name == _END:
        return "(n.recruitment_type != 'PERIOD' OR trim(coalesce(n.recruitment_end_at, '')) != '')"
    return f"trim(coalesce(n.{name}, '')) != ''"


# 자동 전송 조건. 필수 칸 조건에 `AUTO_FIELDS` 가 다 찼는지를 더한다
COMPLETE_SQL = "(" + " AND ".join([READY_SQL, *(_filled_sql(name) for name in AUTO_FIELDS)]) + ")"


def auto_missing(job: Mapping[str, Any] | sqlite3.Row) -> list[str]:
    """자동 전송을 막는 빈 칸 이름. `COMPLETE_SQL` 과 같은 판정을 파이썬으로 한다. 화면이 읽는다."""

    def filled(name: str) -> bool:
        return bool(str(job[name] or "").strip())

    empty: list[str] = []
    for name in AUTO_FIELDS:
        if name == _COMPANY:
            ok = filled("company_name") or filled("parent_company_name")
        elif name == _END:
            ok = job["recruitment_type"] != "PERIOD" or filled(_END)
        else:
            ok = filled(name)
        if not ok:
            empty.append(name)
    return empty


def pending(
    conn: sqlite3.Connection, limit: int, now: str, *, complete: bool = True
) -> list[sqlite3.Row]:
    """보낼 공고. 아직 보내지 않은 것이 먼저이고, 실패한 것은 `MAX_ATTEMPTS` 전까지 다시 고른다.

    `complete` 면 자동 전송 조건(`COMPLETE_SQL`)을, 아니면 필수 칸만(`READY_SQL`) 본다.

    필수 칸이 빈 공고는 여기서 빼 1회 상한을 차지하지 않게 한다 — 분류 전 공고가 수백 건 쌓여 있으면
    그것들이 매번 앞자리를 먹는다. 목록 밖 값처럼 SQL 로 가를 수 없는 것은 보내기 전에 걸러 실패로
    적는다.
    """
    return conn.execute(
        f"""
        SELECT n.* FROM normalized_jobs n
          LEFT JOIN spring_deliveries d ON d.source_url = n.source_url
         WHERE {_UNSENT} AND {_OPEN} AND {COMPLETE_SQL if complete else _READY}
         ORDER BY d.source_url IS NOT NULL, n.id
         LIMIT ?
        """,
        (MAX_ATTEMPTS, now, limit),
    ).fetchall()


def pending_count(conn: sqlite3.Connection, now: str) -> int:
    """자동 전송을 기다리는 공고 수. 자동 전송의 `pending` 과 같은 조건이다. 대시보드가 읽는다."""
    row = conn.execute(
        f"""
        SELECT count(*) AS n FROM normalized_jobs n
          LEFT JOIN spring_deliveries d ON d.source_url = n.source_url
         WHERE {_UNSENT} AND {_OPEN} AND {COMPLETE_SQL}
        """,
        (MAX_ATTEMPTS, now),
    ).fetchone()
    return int(row["n"])


def unready_count(conn: sqlite3.Connection, now: str) -> int:
    """분류는 끝났는데 필수 칸이 비어 보내지 못하는 공고 수. 대시보드가 확인할 것으로 알린다.

    아직 분류하지 않은 공고는 세지 않는다 — 그 공고는 분류 대기이지 칸이 빈 것이 아니다.
    """
    row = conn.execute(
        f"""
        SELECT count(*) AS n FROM normalized_jobs n
          LEFT JOIN spring_deliveries d ON d.source_url = n.source_url
         WHERE d.source_url IS NULL AND {_OPEN} AND NOT {_READY}
           AND EXISTS (SELECT 1 FROM job_classifications c WHERE c.raw_job_id = n.raw_job_id)
        """,
        (now,),
    ).fetchone()
    return int(row["n"])


async def deliver_pending(
    conn: sqlite3.Connection,
    *,
    settings: Settings | None = None,
    force: bool = False,
) -> DeliveryResult:
    """보낼 공고를 1회 상한만큼 오공고에 등록한다. `force` 면 꺼져 있어도 보낸다(`지금 보내기`)."""
    resolved = settings or get_settings()
    config = store.read_config(conn)
    key = resolved.ogonggo_internal_api_key.strip()
    if not config.enabled and not force:
        return DeliveryResult(reason="분류 뒤 전송이 꺼져 있다")
    if not config.configured:
        return DeliveryResult(reason="오공고 주소가 비어 있다")
    if not key:
        return DeliveryResult(reason="OGONGGO_INTERNAL_API_KEY 가 비어 있다. 크롤러 .env 에 넣는다")
    if not _running.acquire(blocking=False):
        return DeliveryResult(reason="이미 보내는 중이다")
    try:
        result = DeliveryResult()
        # 자동 전송(분류 뒤)은 칸이 다 찬 공고만, 사람이 누른 `지금 보내기` 는 필수 칸만 본다
        rows = pending(conn, config.batch_size, _now(resolved), complete=not force)
        if not rows:
            return result
        async with httpx.AsyncClient(
            base_url=config.url.strip().rstrip("/"),
            headers={API_KEY_HEADER: key},
            timeout=TIMEOUT_SECONDS,
            transport=transport,
        ) as client:
            for row in rows:
                await _deliver_one(conn, client, row, result)
        logger.info("오공고 전송: 등록 %s건, 실패 %s건", result.sent, result.failed)
        return result
    finally:
        _running.release()


async def deliver_ids(
    conn: sqlite3.Connection,
    normalized_ids: Sequence[int],
    *,
    settings: Settings | None = None,
) -> DeliveryResult:
    """운영자가 고른 공고를 지금 보낸다 — 공고 목록의 `오공고로 보내기` (2026-09-17, LC-3344).

    사람이 골라 누른 것이라 켜기·끄기, 시도 상한, 마감 여부를 보지 않는다. 오공고가 마감을
    거절하면 그 사유가 실패로 남는다. 이미 보낸 공고는 건너뛴다 — 오공고에는 고치는 경로가 없어
    다시 보내도 409 로 같은 공고를 가리킬 뿐이다.
    """
    resolved = settings or get_settings()
    config = store.read_config(conn)
    key = resolved.ogonggo_internal_api_key.strip()
    if not config.configured:
        return DeliveryResult(reason="오공고 주소가 비어 있다. 설정 > 오공고 전송에서 넣는다")
    if not key:
        return DeliveryResult(reason="OGONGGO_INTERNAL_API_KEY 가 비어 있다. 크롤러 .env 에 넣는다")
    wanted = list(dict.fromkeys(int(value) for value in normalized_ids))
    if not wanted:
        return DeliveryResult(reason="고른 공고가 없다")
    if not _running.acquire(blocking=False):
        return DeliveryResult(reason="이미 보내는 중이다. 잠시 뒤 다시 누른다")
    try:
        result = DeliveryResult()
        marks = ",".join("?" for _ in wanted)
        rows = conn.execute(
            f"""
            SELECT n.* FROM normalized_jobs n
             WHERE n.id IN ({marks})
               AND NOT EXISTS (SELECT 1 FROM spring_deliveries d
                                WHERE d.source_url = n.source_url AND d.status = 'sent')
             ORDER BY n.id
            """,
            wanted,
        ).fetchall()
        if not rows:
            return result
        async with httpx.AsyncClient(
            base_url=config.url.strip().rstrip("/"),
            headers={API_KEY_HEADER: key},
            timeout=TIMEOUT_SECONDS,
            transport=transport,
        ) as client:
            for row in rows:
                await _deliver_one(conn, client, row, result)
        logger.info("오공고 전송(골라 보냄): 등록 %s건, 실패 %s건", result.sent, result.failed)
        return result
    finally:
        _running.release()


async def deliver_after_classify(conn: sqlite3.Connection, settings: Settings | None) -> None:
    """분류 배치가 끝난 뒤 보낸다. 전송이 실패해도 분류는 성공이다 — 예외를 올리지 않는다."""
    try:
        result = await deliver_pending(conn, settings=settings)
    except Exception:
        logger.exception("분류 뒤 오공고 전송이 예외로 끝났다")
        return
    if result.reason:
        logger.info("분류 뒤 오공고로 보내지 않았다: %s", result.reason)


def overview(conn: sqlite3.Connection, limit: int = 20) -> Overview:
    """보낸 수·실패 수와 최근 실패. 읽기 전용이다."""
    counts = {
        str(row["status"]): int(row["n"])
        for row in conn.execute(
            "SELECT status, count(*) AS n FROM spring_deliveries GROUP BY status"
        )
    }
    failures = [
        Failure(
            str(row["source_url"]),
            int(row["attempts"]),
            str(row["last_error"]),
            str(row["updated_at"]),
        )
        for row in conn.execute(
            "SELECT source_url, attempts, last_error, updated_at FROM spring_deliveries"
            " WHERE status = 'failed' ORDER BY updated_at DESC, source_url LIMIT ?",
            (limit,),
        )
    ]
    return Overview(counts.get(SENT, 0), counts.get(FAILED, 0), failures)


async def _deliver_one(
    conn: sqlite3.Connection, client: httpx.AsyncClient, row: sqlite3.Row, result: DeliveryResult
) -> None:
    source_url = str(row["source_url"])
    body = payload(row)
    lacking = missing(body)
    if lacking:
        _fail(conn, source_url, f"필수 칸이 비었거나 목록 밖 값이다: {', '.join(lacking)}", result)
        return
    try:
        response = await client.post(JOBS_PATH, json=body)
        if response.status_code == 409:
            response = await client.get(JOBS_PATH, params={"sourceUrl": source_url})
        if response.status_code in (200, 201):
            job_id = int(response.json()["data"]["jobId"])
            _record(conn, source_url, job_id, SENT, "")
            result.sent += 1
            return
        error = f"{response.status_code} {_message(response)}"
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        error = f"{type(exc).__name__}: {exc}"
    _fail(conn, source_url, error, result)


def _fail(conn: sqlite3.Connection, source_url: str, error: str, result: DeliveryResult) -> None:
    logger.warning("오공고로 보내지 못했다 %s: %s", source_url, error)
    _record(conn, source_url, None, FAILED, error)
    result.failed += 1
    result.errors.append(f"{source_url}: {error}")


def _record(
    conn: sqlite3.Connection, source_url: str, job_id: int | None, status: str, error: str
) -> None:
    conn.execute(
        """
        INSERT INTO spring_deliveries (source_url, spring_job_id, status, attempts, last_error,
                                       sent_at)
        VALUES (?, ?, ?, 1, ?, CASE WHEN ? = 'sent' THEN datetime('now') END)
        ON CONFLICT (source_url) DO UPDATE
           SET spring_job_id = coalesce(excluded.spring_job_id, spring_job_id),
               status = excluded.status,
               attempts = attempts + 1,
               last_error = excluded.last_error,
               sent_at = coalesce(excluded.sent_at, sent_at),
               updated_at = datetime('now')
        """,
        (source_url, job_id, status, error[:500], status),
    )


def _message(response: httpx.Response) -> str:
    """오공고 오류 응답의 `message`. 읽지 못하면 본문 앞부분이다."""
    try:
        data = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(data, dict) and isinstance(data.get("message"), str):
        return str(data["message"])
    return response.text[:200]


def _now(settings: Settings) -> str:
    try:
        zone: Any = ZoneInfo(settings.display_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = UTC
    return datetime.now(zone).strftime("%Y-%m-%d %H:%M:%S")


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _choice(job: Mapping[str, Any] | sqlite3.Row, name: str) -> str | None:
    value = _text(job[name])
    return value if value in VALUE_LABELS[name] else None


def _integer(value: Any) -> int | None:
    text = _text(value)
    if text is None or not _INTEGER.fullmatch(text):
        return None
    number = int(text)
    return number if number >= 1 else None


def _boolean(value: Any) -> bool | None:
    text = _text(value)
    if text == "true":
        return True
    if text == "false":
        return False
    return None


def _datetime(value: Any) -> str | None:
    text = _text(value)
    if text is None or not _DATETIME.fullmatch(text):
        return None
    return text.replace(" ", "T")
