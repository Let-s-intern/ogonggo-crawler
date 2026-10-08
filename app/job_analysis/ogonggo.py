"""오공고 서버와 주고받는 공고 분석 API (2026-10-08 결정, LC-3446).

분석 대상을 고르는 것은 오공고 서버다. 크롤러는 받아서 분석하고 돌려준다. 서버는 본문 해시로 분석이
낡았는지 알고, 크롤러는 받은 해시를 결과에 그대로 실어 보낸다 — 그사이 운영자가 본문을 고쳤으면
서버가 409 로 거절한다.

## 계약

`GET /api/v1/internal/jobs/analysis-targets?size=N`

게시 중인 공고 가운데 분석이 없거나 본문 해시가 바뀐 것. 응답은 `{status, message, data}` 이고
`data` 는 목록이다. 한 항목은 크롤러 전송 본문과 같은 칸 이름에 `jobId`·`source`·`contentHash` 를
더한 모양이다.

```json
{"jobId": 12, "source": "WORK24", "contentHash": "…", "title": "…", "companyName": "…",
 "employmentType": "FULL_TIME", "recruitmentType": "PERIOD", "recruitmentEndAt": "…",
 "sourceUrl": "…", "responsibilities": "…", … 본문 여덟 칸}
```

`PUT /api/v1/internal/jobs/{jobId}/analysis`

```json
{"contentHash": "…", "guideVersion": 3, "model": "deepseek-…", "analysis": {…}}
```

200 받음 · 404 공고가 없어졌다 · 409 본문이 바뀌었다. 인증은 공고 전송과 같은 `X-Internal-Api-Key`
다.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.deliver import settings as deliver_store
from app.deliver.spring import API_KEY_HEADER, JOBS_PATH, TIMEOUT_SECONDS

TARGETS_PATH = f"{JOBS_PATH}/analysis-targets"

# 테스트가 `httpx.MockTransport` 를 끼우는 자리
transport: httpx.AsyncBaseTransport | None = None

SOURCE_LABELS: dict[str, str] = {"CRAWLER": "크롤러", "WORK24": "고용24", "COMPANY": "직접 등록"}

SENT = "sent"
NOT_FOUND = "not_found"
STALE = "stale"
FAILED = "failed"


class OgonggoUnavailable(RuntimeError):
    """오공고 서버를 부를 수 없다. 설정이 비었거나 서버가 답하지 않는다."""


@dataclass(frozen=True)
class Target:
    """분석할 공고 하나. `job` 은 오공고 요청 칸 이름으로 된 공고다."""

    job_id: int
    source: str
    content_hash: str
    job: dict[str, Any]


@dataclass(frozen=True)
class PutResult:
    outcome: str
    message: str = ""


def connection(conn: sqlite3.Connection, settings: Settings | None = None) -> httpx.AsyncClient:
    """오공고 서버 클라이언트. 주소나 키가 비었으면 사유를 올린다."""
    resolved = settings or get_settings()
    config = deliver_store.read_config(conn)
    key = resolved.ogonggo_internal_api_key.strip()
    if not config.configured:
        raise OgonggoUnavailable("오공고 주소가 비어 있다. 설정 > 오공고 전송에서 넣는다")
    if not key:
        raise OgonggoUnavailable("OGONGGO_INTERNAL_API_KEY 가 비어 있다. 크롤러 .env 에 넣는다")
    return httpx.AsyncClient(
        base_url=config.url.strip().rstrip("/"),
        headers={API_KEY_HEADER: key},
        timeout=TIMEOUT_SECONDS,
        transport=transport,
    )


async def targets(client: httpx.AsyncClient, size: int) -> list[Target]:
    try:
        response = await client.get(TARGETS_PATH, params={"size": size})
    except httpx.HTTPError as exc:
        raise OgonggoUnavailable(f"분석 대상을 받지 못했다: {exc}") from exc
    if response.status_code != 200:
        raise OgonggoUnavailable(f"분석 대상을 받지 못했다({response.status_code})")
    data = response.json().get("data") or []
    found: list[Target] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, Mapping) or not isinstance(item.get("jobId"), int):
            continue
        found.append(
            Target(
                job_id=int(item["jobId"]),
                source=str(item.get("source") or ""),
                content_hash=str(item.get("contentHash") or ""),
                job=dict(item),
            )
        )
    return found


async def put(
    client: httpx.AsyncClient,
    job_id: int,
    *,
    content_hash: str,
    analysis: Mapping[str, Any],
    guide_version: int | None,
    model: str,
) -> PutResult:
    body = {
        "contentHash": content_hash,
        "guideVersion": guide_version,
        "model": model,
        "analysis": analysis,
    }
    try:
        response = await client.put(f"{JOBS_PATH}/{job_id}/analysis", json=body)
    except httpx.HTTPError as exc:
        return PutResult(FAILED, f"보내지 못했다: {exc}")
    if response.status_code in (200, 201, 204):
        return PutResult(SENT)
    if response.status_code == 404:
        return PutResult(NOT_FOUND, "오공고에서 공고가 없어졌다")
    if response.status_code == 409:
        return PutResult(STALE, "그사이 공고 본문이 바뀌었다. 다음 실행에서 다시 분석한다")
    return PutResult(FAILED, f"오공고가 받지 않았다({response.status_code}): {response.text[:300]}")
