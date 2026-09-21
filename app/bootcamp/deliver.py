"""채운 부트캠프를 오공고 관리자 API 로 등록·교체한다 (2026-09-22 결정, LC-3364).

`POST /api/v1/internal/bootcamps` 로 등록하고 받은 id 를 행에 적는다. 공고 전송과 달리 **바뀐 과정은
다시 보낸다** — 새싹은 모집기간을 늘리며 안내를 갈아 끼우고, 부트캠프는 건수가 적어 교체가 싸다.
id 가 있으면 `PUT /{id}` 로 통째로 바꾼다. 409 가 오면 `GET ?sourceUrl=` 로 id 를 찾아 교체한다.

오공고 주소와 키는 공고 전송과 같은 것이다 (`app/deliver/settings.py`, `OGONGGO_INTERNAL_API_KEY`).

## 보내지 않는 과정

- AI 가 아직 채우지 않았거나 채운 뒤 안내가 바뀐 과정(`filled_hash != page_hash`).
  다음 수집이 채운다
- 이미 지금 안내로 보낸 과정(`sent_hash = page_hash`)
- 지금 안내로 세 번 실패한 과정. 안내가 바뀌면 다시 센다

## 고정 값

새싹 오프라인 과정은 모두 서울시가 비용을 대는 무료 과정이고, 수강신청은 새싹 사이트의
과정 페이지에서 한다. 그래서 진행 방식은 오프라인, 수강료는 무료, 지원 방법은 외부
페이지(원문 주소)로 고정한다.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import httpx

from app.bootcamp import store
from app.bootcamp.sesac import CurriculumGroup
from app.config import Settings, get_settings
from app.deliver import settings as deliver_store
from app.deliver.spring import API_KEY_HEADER, TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

BOOTCAMPS_PATH = "/api/v1/internal/bootcamps"
MAX_ATTEMPTS = 3
# 차시 묶음이 이보다 많으면 주차별로 합친다. 은평 이커머스 과정은 하루짜리 차시가 48개다
MAX_CURRICULUM_GROUPS = 12
SUBTITLE_LIMIT = 255

# 테스트가 `httpx.MockTransport` 를 끼우는 자리
transport: httpx.AsyncBaseTransport | None = None

_running = threading.Lock()

_READY = """
    filled_hash = page_hash
    AND coalesce(content, '') <> ''
    AND coalesce(short_description, '') <> ''
"""
_UNSENT = "(sent_hash IS NULL OR sent_hash <> page_hash)"


@dataclass
class BootcampDeliveryResult:
    sent: int = 0
    failed: int = 0
    reason: str = ""
    errors: list[str] = field(default_factory=list)


def pending(conn: sqlite3.Connection, *, retry_failed: bool = False) -> list[sqlite3.Row]:
    """보낼 과정. `retry_failed` 면 시도 상한을 넘은 것도 고른다 — 사람이 누른 `지금 보내기` 다."""
    limit = "" if retry_failed else f" AND send_attempts < {MAX_ATTEMPTS}"
    return conn.execute(
        f"SELECT * FROM bootcamps WHERE {_READY} AND {_UNSENT}{limit} ORDER BY id"
    ).fetchall()


def pending_count(conn: sqlite3.Connection) -> int:
    return len(pending(conn))


async def deliver_pending(
    conn: sqlite3.Connection,
    *,
    settings: Settings | None = None,
    retry_failed: bool = False,
) -> BootcampDeliveryResult:
    resolved = settings or get_settings()
    config = deliver_store.read_config(conn)
    key = resolved.ogonggo_internal_api_key.strip()
    if not config.configured:
        return BootcampDeliveryResult(
            reason="오공고 주소가 비어 있다. 설정 > 오공고 전송에서 넣는다"
        )
    if not key:
        return BootcampDeliveryResult(
            reason="OGONGGO_INTERNAL_API_KEY 가 비어 있다. 크롤러 .env 에 넣는다"
        )
    if not _running.acquire(blocking=False):
        return BootcampDeliveryResult(reason="이미 보내는 중이다")
    try:
        result = BootcampDeliveryResult()
        rows = pending(conn, retry_failed=retry_failed)
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
        logger.info("부트캠프 오공고 전송: %s건 보냄, %s건 실패", result.sent, result.failed)
        return result
    finally:
        _running.release()


def payload(row: sqlite3.Row) -> dict[str, Any]:
    """오공고 크롤러 부트캠프 요청 본문. 칸 이름은 오공고 `Bootcamp` 엔티티 그대로다."""
    start = row["recruitment_start_date"]
    end = row["recruitment_end_date"]
    campus = str(row["campus"] or "").strip()
    return {
        "companyName": f"새싹 {campus}캠퍼스" if campus else "새싹(SeSAC)",
        "title": str(row["title"]),
        "programType": str(row["category"] or "").strip() or "기타",
        "operationType": "OFFLINE",
        # 모집 마감일이 없으면 상시 모집이다. 상시 모집에는 마감 일시를 둘 수 없다
        "recruitmentType": "PERIOD" if start and end else "ALWAYS_OPEN",
        "recruitmentStartAt": f"{start}T00:00:00" if start else None,
        "recruitmentEndAt": f"{end}T23:59:59" if start and end else None,
        "programStartDate": str(row["program_start_date"]),
        "programEndDate": str(row["program_end_date"]),
        "capacity": row["capacity"],
        "tuitionType": "FREE",
        "tuitionAmount": None,
        "representativeImageUrl": str(row["thumbnail_url"] or ""),
        "shortDescription": str(row["short_description"] or ""),
        "content": str(row["content"] or ""),
        "eligibilityAndSelectionProcess": row["eligibility"] or None,
        "applicationMethod": "EXTERNAL_PAGE",
        "applicationUrl": str(row["source_url"]),
        "managerEmail": row["manager_email"] or None,
        "inquiryUrl": None,
        "sourceUrl": str(row["source_url"]),
        "curriculums": curriculums(
            store.curriculum(row), date.fromisoformat(str(row["program_start_date"]))
        ),
    }


def curriculums(groups: list[CurriculumGroup], program_start: date) -> list[dict[str, Any]]:
    """차시 묶음을 교육 시작일부터 센 주차로 바꾼다. 날짜가 없는 묶음은 뺀다.

    묶음이 `MAX_CURRICULUM_GROUPS` 보다 많으면 같은 주차에 시작하는 묶음을 하나로 합친다. 하루짜리
    차시가 수십 개인 과정을 그대로 보내면 목차가 수업 목록이 된다.
    """
    dated = [
        (group.name, _week(group.first_day, program_start), _week(group.last_day, program_start))
        for group in groups
        if group.first_day and group.last_day
    ]
    items = [(_strip_order(name), first, last) for name, first, last in dated]
    if len(items) > MAX_CURRICULUM_GROUPS:
        merged: dict[int, tuple[list[str], int]] = {}
        for name, first, last in items:
            names, end = merged.get(first, ([], first))
            if name not in names:
                names.append(name)
            merged[first] = (names, max(end, last))
        items = [(" · ".join(names), first, end) for first, (names, end) in merged.items()]
    return [
        {"startWeek": first, "endWeek": last, "subtitle": name[:SUBTITLE_LIMIT]}
        for name, first, last in items
        if name
    ]


def _week(day: date | None, program_start: date) -> int:
    if day is None:
        return 1
    return max(1, (day - program_start).days // 7 + 1)


def _strip_order(name: str) -> str:
    """`3차시 Backend` 의 `3차시` 를 뗀다. 순서는 배열 순서가 말한다."""
    head, _, rest = name.partition(" ")
    return rest.strip() if head.endswith("차시") and rest.strip() else name.strip()


async def _deliver_one(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    row: sqlite3.Row,
    result: BootcampDeliveryResult,
) -> None:
    source_url = str(row["source_url"])
    body = payload(row)
    if not body["representativeImageUrl"]:
        _fail(conn, row, "대표 이미지가 없다", result)
        return
    try:
        bootcamp_id = row["spring_bootcamp_id"]
        if bootcamp_id is None:
            response = await client.post(BOOTCAMPS_PATH, json=body)
            if response.status_code == 409:
                found = await client.get(BOOTCAMPS_PATH, params={"sourceUrl": source_url})
                if found.status_code != 200:
                    raise _Rejected(f"{found.status_code} {_message(found)}")
                bootcamp_id = int(found.json()["data"]["bootcampId"])
                response = await client.put(f"{BOOTCAMPS_PATH}/{bootcamp_id}", json=body)
            elif response.status_code in (200, 201):
                bootcamp_id = int(response.json()["data"]["bootcampId"])
        else:
            response = await client.put(f"{BOOTCAMPS_PATH}/{bootcamp_id}", json=body)
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
        UPDATE bootcamps
           SET spring_bootcamp_id = ?, sent_hash = page_hash, send_status = 'sent',
               send_attempts = send_attempts + 1, send_error = '', sent_at = datetime('now')
         WHERE id = ?
        """,
        (bootcamp_id, row["id"]),
    )
    result.sent += 1


class _Rejected(Exception):
    """오공고가 2xx 가 아닌 답을 줬다. 사유는 오공고가 돌려준 그대로다."""


def _fail(
    conn: sqlite3.Connection, row: sqlite3.Row, error: str, result: BootcampDeliveryResult
) -> None:
    logger.warning("부트캠프를 오공고로 보내지 못했다 %s: %s", row["source_url"], error)
    conn.execute(
        """
        UPDATE bootcamps
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
