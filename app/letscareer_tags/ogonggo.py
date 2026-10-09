"""오공고 서버와 주고받는 렛츠커리어 콘텐츠 태그 API (2026-10-09 결정, LC-3448).

공고 분석(`app/job_analysis/ogonggo.py`)과 같은 짜임이다. 대상을 고르는 것은 서버이고, 크롤러는 받은
해시를 태그에 그대로 실어 보낸다. 그사이 렛츠커리어에서 내용이 바뀌었으면 서버가 409 로 거절한다.

## 계약

`GET /api/v1/internal/lets-career-contents/tag-targets?size=N`

```json
{"contentId": 3, "kind": "MATERIAL", "category": "MATERIAL", "title": "…", "description": "…",
 "labels": ["마케팅"], "contentHash": "…"}
```

`PUT /api/v1/internal/lets-career-contents/{contentId}/tags`

```json
{"contentHash": "…", "jobFields": ["MARKETING_ADVERTISING"], "jobRoles": [],
 "topics": ["PERSONAL_STATEMENT"]}
```

200 받음 · 404 렛츠커리어 목록에서 빠졌다 · 409 내용이 바뀌었다. 인증은 공고 전송과 같은
`X-Internal-Api-Key` 다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.job_analysis.ogonggo import FAILED, NOT_FOUND, SENT, STALE, OgonggoUnavailable, PutResult

CONTENTS_PATH = "/api/v1/internal/lets-career-contents"
TARGETS_PATH = f"{CONTENTS_PATH}/tag-targets"

__all__ = ["FAILED", "NOT_FOUND", "SENT", "STALE", "OgonggoUnavailable", "PutResult", "Target"]


@dataclass(frozen=True)
class Target:
    """태그할 콘텐츠 하나."""

    content_id: int
    kind: str
    category: str
    title: str
    description: str
    content_hash: str
    labels: tuple[str, ...] = field(default=())


async def targets(client: httpx.AsyncClient, size: int) -> list[Target]:
    try:
        response = await client.get(TARGETS_PATH, params={"size": size})
    except httpx.HTTPError as exc:
        raise OgonggoUnavailable(f"태그 대상을 받지 못했다: {exc}") from exc
    if response.status_code != 200:
        raise OgonggoUnavailable(f"태그 대상을 받지 못했다({response.status_code})")
    data = response.json().get("data") or []
    found: list[Target] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, Mapping) or not isinstance(item.get("contentId"), int):
            continue
        labels = item.get("labels")
        found.append(
            Target(
                content_id=int(item["contentId"]),
                kind=str(item.get("kind") or ""),
                category=str(item.get("category") or ""),
                title=str(item.get("title") or ""),
                description=str(item.get("description") or ""),
                content_hash=str(item.get("contentHash") or ""),
                labels=tuple(str(label) for label in labels) if isinstance(labels, list) else (),
            )
        )
    return found


async def put(
    client: httpx.AsyncClient, content_id: int, *, content_hash: str, tags: Mapping[str, Any]
) -> PutResult:
    body = {"contentHash": content_hash, **tags}
    try:
        response = await client.put(f"{CONTENTS_PATH}/{content_id}/tags", json=body)
    except httpx.HTTPError as exc:
        return PutResult(FAILED, f"보내지 못했다: {exc}")
    if response.status_code in (200, 201, 204):
        return PutResult(SENT)
    if response.status_code == 404:
        return PutResult(NOT_FOUND, "렛츠커리어 목록에서 빠졌다")
    if response.status_code == 409:
        return PutResult(STALE, "그사이 내용이 바뀌었다. 다음 실행에서 다시 태그한다")
    return PutResult(FAILED, f"오공고가 받지 않았다({response.status_code}): {response.text[:300]}")
