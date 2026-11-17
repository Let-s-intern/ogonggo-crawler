"""오공고 서버와 주고받는 상시 채용 마감 여부 확인 API (LC-3621).

## 계약

`GET /api/v1/internal/jobs/source-check-targets?afterId=N&size=M`

게시 중인 모집 중 상시 채용 공고 가운데 원문 URL 이 있는 것을 `afterId` 다음부터 식별자 순으로 준다.
등록 경로(크롤링·고용24·기업회원)를 가리지 않는다. 빈 목록이면 끝이다.

```json
{"jobId": 12, "source": "WORK24", "sourceUrl": "…", "companyName": "…", "title": "…"}
```

`POST /api/v1/internal/jobs/{jobId}/source-check-close`

모집을 마감한다. 반복해도 200 이고, 없거나 지워진 공고는 404 다. 크롤링 공고만 받는 목록 대조
마감(`/{jobId}/close`)과 경로가 다르다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from app.deliver.spring import JOBS_PATH
from app.job_analysis.ogonggo import OgonggoUnavailable

TARGETS_PATH = f"{JOBS_PATH}/source-check-targets"
CLOSE_PATH = JOBS_PATH + "/{job_id}/source-check-close"


@dataclass(frozen=True)
class Target:
    job_id: int
    source: str
    source_url: str
    company: str
    title: str


async def targets(client: httpx.AsyncClient, after_id: int, size: int) -> list[Target]:
    try:
        response = await client.get(TARGETS_PATH, params={"afterId": after_id, "size": size})
    except httpx.HTTPError as exc:
        raise OgonggoUnavailable(f"마감 여부 확인 대상을 받지 못했다: {exc}") from exc
    if response.status_code != 200:
        raise OgonggoUnavailable(f"마감 여부 확인 대상을 받지 못했다({response.status_code})")
    data = response.json().get("data") or []
    found: list[Target] = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, Mapping) or not isinstance(item.get("jobId"), int):
            continue
        url = str(item.get("sourceUrl") or "").strip()
        if not url:
            continue
        found.append(
            Target(
                job_id=int(item["jobId"]),
                source=str(item.get("source") or ""),
                source_url=url,
                company=str(item.get("companyName") or ""),
                title=str(item.get("title") or ""),
            )
        )
    return found


async def close(client: httpx.AsyncClient, job_id: int) -> str:
    """공고를 마감한다. 성공하면 빈 글자, 실패하면 사유다. 오공고에 없으면 끝난 것으로 본다."""
    try:
        response = await client.post(CLOSE_PATH.format(job_id=job_id))
    except httpx.HTTPError as exc:
        return f"마감을 보내지 못했다: {exc}"
    if response.status_code in (200, 204, 404):
        return ""
    return f"오공고가 마감하지 않았다({response.status_code}): {response.text[:200]}"
