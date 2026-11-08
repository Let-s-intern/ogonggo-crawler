"""슬랙 웹훅으로 글 하나를 보낸다.

웹훅 주소는 그 자체로 비밀이라 로그와 결과 문구에 적지 않는다.
"""

from __future__ import annotations

import logging

import httpx

from app.notify.ntfy import TIMEOUT_SECONDS, SendResult

logger = logging.getLogger(__name__)


async def send(
    webhook_url: str,
    text: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SendResult:
    """슬랙 웹훅으로 한 건 보낸다. 어떤 실패에서도 예외를 던지지 않는다."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(TIMEOUT_SECONDS), transport=transport
        ) as client:
            response = await client.post(webhook_url, json={"text": text})
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        logger.warning("슬랙이 %s 로 거절했다", status)
        return SendResult(ok=False, detail=f"슬랙이 {status} 로 거절했다", status_code=status)
    except Exception as exc:
        # 예외 문구에 웹훅 주소가 섞일 수 있어 종류만 남긴다
        logger.warning("슬랙으로 보내지 못했다: %s", type(exc).__name__)
        return SendResult(ok=False, detail=f"슬랙으로 보내지 못했다: {type(exc).__name__}")
    return SendResult(ok=True, detail="슬랙으로 보냈다", status_code=response.status_code)
