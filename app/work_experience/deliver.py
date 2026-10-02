"""정리한 미래내일 일경험 프로그램을 오공고 채용공고로 등록·교체한다 (2026-10-02 결정, LC-3432).

공고 전송과 같은 크롤러 채용공고 API 를 쓴다 (`app/deliver/spring.py`). `POST /api/v1/internal/jobs`
로 등록하고 받은 id 를 행에 적는다. 보낸 뒤 읽은 값이 바뀌면(모집기간이 늘었거나 인원이 바뀌었으면)
`PUT /{id}` 로 교체한다. 409 가 오면 `GET ?sourceUrl=` 로 id 를 찾아 교체한다 — id 를 적기 전에
크롤러가 죽었거나 DB 를 옮긴 경우다.

오공고 주소와 키는 공고 전송과 같은 것이다 (`app/deliver/settings.py`, `OGONGGO_INTERNAL_API_KEY`).

## 보내지 않는 프로그램

- AI 가 아직 채우지 않은 프로그램(`filled_hash != page_hash`). 다음 수집이 채운다
- 이미 지금 값으로 보낸 프로그램(`sent_hash = page_hash`). 보낸 뒤 참여기업 로고를 올렸으면
  다시 보낸다
- 모집 마감일이 지난 프로그램. 오공고가 마감일로 모집을 닫으므로 이미 보낸 것도 다시 보내지 않는다
- 세 번 연달아 실패한 프로그램. 화면의 `지금 보내기` 는 보낸다. 보내는 데 성공하면 다시 센다

## 고정 값

| 칸 | 값 | 왜 |
|---|---|---|
| 고용 형태 | 미래내일 일경험(`WORK_EXPERIENCE`) | 사용자 결정 (2026-10-02) |
| 회사 | 첫 참여기업. 카드에 없으면 운영기관 | 청년이 일하는 곳이다. 모회사는 없다 |
| 시·도 | 카드의 지역. `지역무관` 은 전국 | 포털 지역 이름이 오공고 시·도 화면 이름과 같다 |
| 모집 유형 | 기간 채용, 마감일에 자동 종료 | 모집기간이 늘 있다 |
| 채용 시 마감 | 아니다 | 마감일까지 신청을 받고 그 뒤에 서류·면접으로 뽑는다 |
| 지원 방법 | 외부 페이지(원문 주소) | 신청은 포털에서 한다 |
| 최소 경력 연수 | 신입이면 0, 그 밖은 비움 | 공고 정규화와 같은 규칙이다 (`settle_fields`) |

채용 안내사항(`recruitmentNotice`) 앞에는 프로그램 유형·일경험 기간·사전직무교육·운영기관 연락처를
파서가 읽은 그대로 붙인다. 청년이 신청 전에 꼭 알아야 하는데 공고 칸에는 자리가 없다.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app import job_roles
from app.config import Settings, get_settings
from app.deliver import settings as deliver_store
from app.deliver.spring import API_KEY_HEADER, JOBS_PATH, TIMEOUT_SECONDS
from app.work_experience import portal, store
from app.work_experience.fill import ProgramFill, region_name

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
EMPLOYMENT_TYPE = "WORK_EXPERIENCE"
TITLE_LIMIT = 255
COMPANY_LIMIT = 150
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

# 테스트가 `httpx.MockTransport` 를 끼우는 자리
transport: httpx.AsyncBaseTransport | None = None

_running = threading.Lock()

_READY = "filled_hash = page_hash AND fill_json IS NOT NULL"
_UNSENT = "(sent_hash IS NULL OR sent_hash <> page_hash)"


@dataclass
class WorkExperienceDeliveryResult:
    sent: int = 0
    failed: int = 0
    reason: str = ""
    errors: list[str] = field(default_factory=list)


def pending(
    conn: sqlite3.Connection, today: str, *, retry_failed: bool = False
) -> list[sqlite3.Row]:
    """보낼 프로그램. `retry_failed` 면 시도 상한을 넘은 것도 고른다 — 사람이 누른 `지금 보내기` 다.

    보낸 뒤 로고가 바뀐 프로그램도 고른다. 로고는 회사 표에 있어 SQL 한 줄로 비교하지 않는다.
    """
    limit = "" if retry_failed else f" AND send_attempts < {MAX_ATTEMPTS}"
    rows = conn.execute(
        f"SELECT *, {_UNSENT} AS unsent FROM work_experiences"
        f" WHERE {_READY} AND recruitment_end_date >= ?{limit} ORDER BY id",
        (today,),
    ).fetchall()
    return [
        row
        for row in rows
        if row["unsent"]
        or (row["sent_logo_url"] or "") != (store.logo_url(conn, str(row["company"])) or "")
    ]


def pending_count(conn: sqlite3.Connection, settings: Settings | None = None) -> int:
    return len(pending(conn, today(settings or get_settings())))


def today(settings: Settings) -> str:
    """표시 시간대의 오늘. 마감일이 오늘인 프로그램은 아직 보낸다."""
    try:
        zone: Any = ZoneInfo(settings.display_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone = UTC
    return datetime.now(zone).date().isoformat()


async def deliver_pending(
    conn: sqlite3.Connection,
    *,
    settings: Settings | None = None,
    retry_failed: bool = False,
) -> WorkExperienceDeliveryResult:
    resolved = settings or get_settings()
    config = deliver_store.read_config(conn)
    key = resolved.ogonggo_internal_api_key.strip()
    if not config.configured:
        return WorkExperienceDeliveryResult(
            reason="오공고 주소가 비어 있다. 설정 > 오공고 전송에서 넣는다"
        )
    if not key:
        return WorkExperienceDeliveryResult(
            reason="OGONGGO_INTERNAL_API_KEY 가 비어 있다. 크롤러 .env 에 넣는다"
        )
    if not _running.acquire(blocking=False):
        return WorkExperienceDeliveryResult(reason="이미 보내는 중이다")
    try:
        result = WorkExperienceDeliveryResult()
        rows = pending(conn, today(resolved), retry_failed=retry_failed)
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
        logger.info("미래내일 일경험 오공고 전송: %s건 보냄, %s건 실패", result.sent, result.failed)
        return result
    finally:
        _running.release()


def payload(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    """오공고 크롤러 채용공고 요청 본문. 칸 이름은 오공고 `Job` 엔티티 그대로다."""
    fill = store.fill_of(row) or ProgramFill()
    company = portal.main_company(str(row["company"] or "")) or str(row["operator"] or "").strip()
    job_field, job_role = job_roles.enum_names(fill.job_field or None, fill.job_role or None)
    region = region_name(str(row["region_label"] or ""))
    start = row["recruitment_start_date"]
    end = row["recruitment_end_date"]
    email = _manager_email(store.sections_of(row))
    return {
        "companyName": company[:COMPANY_LIMIT],
        "parentCompanyName": None,
        "title": str(row["title"])[:TITLE_LIMIT],
        "jobField": job_field,
        "jobRole": job_role,
        "industry": fill.industry or None,
        "coverImageUrl": None,
        "logoUrl": store.logo_url(conn, company),
        "employmentType": EMPLOYMENT_TYPE,
        "experienceType": fill.experience_type,
        "experienceMinYears": 0 if fill.experience_type == "NEWCOMER" else None,
        "educationLevel": fill.education_level,
        "region": region,
        "subRegion": fill.sub_region or None,
        "recruitmentType": "PERIOD",
        "recruitmentHeadcount": row["headcount"],
        "recruitmentStartAt": f"{start}T00:00:00" if start else None,
        "recruitmentEndAt": f"{end}T23:59:59",
        "closesWhenFilled": False,
        "autoCloseEnabled": True,
        "companyAndTeamIntroduction": fill.company_and_team_introduction or None,
        "responsibilities": fill.responsibilities or None,
        "qualifications": fill.qualifications or None,
        "preferredQualifications": fill.preferred_qualifications or None,
        "compensation": fill.compensation or None,
        "benefits": fill.benefits or None,
        "hiringProcess": fill.hiring_process or None,
        "recruitmentNotice": _notice(row, fill),
        "applicationMethod": "EXTERNAL_PAGE",
        "applyEmail": None,
        "inquiryEmail": email,
        "sourceUrl": str(row["source_url"]),
    }


def _notice(row: sqlite3.Row, fill: ProgramFill) -> str:
    """프로그램 안내 묶음 + AI 가 옮긴 안내사항."""
    sections = store.sections_of(row)
    training = sections.get("사전직무교육정보", {})
    contact = sections.get("운영기관담당자정보", {})
    lines = [f"■ 미래내일 일경험 {row['type_label'] or ''}".rstrip()]
    if row["work_start_date"] or row["work_end_date"]:
        lines.append(
            f"- 일경험 기간: {_day(row['work_start_date'])} ~ {_day(row['work_end_date'])}"
        )
    if training.get("사전직무교육기간"):
        place = training.get("교육장소", "")
        lines.append(
            f"- 사전직무교육: {training['사전직무교육기간']}" + (f" ({place})" if place else "")
        )
    operator = contact.get("운영기관") or str(row["operator"] or "")
    if operator:
        reach = ", ".join(
            value for value in (contact.get("담당자", ""), contact.get("전화번호", "")) if value
        )
        lines.append(f"- 운영기관: {operator}" + (f" ({reach})" if reach else ""))
    block = "\n".join(lines)
    return f"{block}\n\n{fill.recruitment_notice}" if fill.recruitment_notice else block


def _manager_email(sections: dict[str, dict[str, str]]) -> str | None:
    value = sections.get("운영기관담당자정보", {}).get("이메일", "").strip()
    return value if _EMAIL.fullmatch(value) and len(value) <= 320 else None


def _day(value: Any) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        return ""


async def _deliver_one(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    row: sqlite3.Row,
    result: WorkExperienceDeliveryResult,
) -> None:
    source_url = str(row["source_url"])
    body = payload(conn, row)
    if not body["companyName"]:
        _fail(conn, row, "참여기업도 운영기관도 없다", result)
        return
    try:
        job_id = row["spring_job_id"]
        if job_id is None:
            response = await client.post(JOBS_PATH, json=body)
            if response.status_code == 409:
                found = await client.get(JOBS_PATH, params={"sourceUrl": source_url})
                if found.status_code != 200:
                    raise _Rejected(f"{found.status_code} {_message(found)}")
                job_id = int(found.json()["data"]["jobId"])
                response = await client.put(f"{JOBS_PATH}/{job_id}", json=body)
            elif response.status_code in (200, 201):
                job_id = int(response.json()["data"]["jobId"])
        else:
            response = await client.put(f"{JOBS_PATH}/{job_id}", json=body)
        if response.status_code not in (200, 201):
            raise _Rejected(f"{response.status_code} {_message(response)}")
    except (_Rejected, httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        _fail(
            conn,
            row,
            str(exc) if isinstance(exc, _Rejected) else f"{type(exc).__name__}: {exc}",
            result,
        )
        return
    conn.execute(
        """
        UPDATE work_experiences
           SET spring_job_id = ?, sent_hash = page_hash, sent_logo_url = ?, send_status = 'sent',
               send_attempts = 0, send_error = '', sent_at = datetime('now')
         WHERE id = ?
        """,
        (job_id, body["logoUrl"] or "", row["id"]),
    )
    result.sent += 1


class _Rejected(Exception):
    """오공고가 2xx 가 아닌 답을 줬다. 사유는 오공고가 돌려준 그대로다."""


def _fail(
    conn: sqlite3.Connection, row: sqlite3.Row, error: str, result: WorkExperienceDeliveryResult
) -> None:
    logger.warning("미래내일 일경험을 오공고로 보내지 못했다 %s: %s", row["source_url"], error)
    conn.execute(
        """
        UPDATE work_experiences
           SET send_status = 'failed', send_attempts = send_attempts + 1, send_error = ?
         WHERE id = ?
        """,
        (error[:500], row["id"]),
    )
    result.failed += 1
    result.errors.append(f"{row['source_url']}: {error}")


def _message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(data, dict) and isinstance(data.get("message"), str):
        return str(data["message"])
    return response.text[:200]
