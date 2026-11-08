"""작업이 끝나면 결과를 슬랙으로 보낸다 (2026-11-02 결정).

서버가 돌린 작업이 어떻게 끝났는지 화면을 열지 않고 알게 한다. 웹훅은 일일 공고 현황과 같은
`DAILY_REPORT_SLACK_WEBHOOK` 이고, 비어 있으면 아무것도 보내지 않는다.

| 작업 | 언제 보내는가 |
|---|---|
| 매일 수집 | 한 바퀴가 끝날 때 (`app/notify/daily_report.py`) |
| 분류·오공고 전송 | 한 바퀴 뒤 남은 분류까지 끝날 때 (`app/notify/daily_report.py`) |
| 부트캠프·일경험 수집 | 묶음을 다 돌았을 때 |
| 렛츠커리어 태그 | 대상이 있었을 때 |
| 공고 분석 | 한 번 돌 때마다 |
| 분류 | 부가 워크플로우와 `POST /api/classify`. 수집 직후 분류는 위 묶음에 든다 |
| 재정규화, AI 다시 채우기, 추가 항목 다시 채우기 | 끝날 때 |
| 수동 1회 실행, 원문 다시 수집, 전체 다시 수집, 사이트 추가 | 끝날 때 |

사이트마다 도는 수집과 수집 직후 분류는 따로 보내지 않는다. 하루 백 통이 오면 아무도 읽지 않는다.
시험 수집과 `지금 보내기` 처럼 화면에서 결과를 바로 받는 요청도 보내지 않는다.

**여기서 예외가 나가지 않는다.** 부르는 자리가 작업의 끝이라, 보고가 실패했다고 끝난 작업이
실패가 되면 안 된다.

이 모듈은 분류·정규화처럼 아래층 모듈이 읽으므로 `app.notify.slack` 과 설정 말고는 읽지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from app.config import get_settings
from app.notify.ntfy import SendResult
from app.notify.slack import send

logger = logging.getLogger(__name__)


def build_text(day: datetime, name: str, lines: Sequence[str], *, ok: bool = True) -> str:
    title = f"*{day.month}/{day.day} {name} {'완료' if ok else '실패'}*"
    return "\n".join([title, *(f"• {line}" for line in lines)])


async def report(
    name: str,
    lines: Sequence[str],
    *,
    ok: bool = True,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SendResult | None:
    """작업 결과 한 통을 보낸다. 웹훅이 비어 있으면 None 이다."""
    try:
        settings = get_settings()
        webhook = settings.daily_report_slack_webhook
        if not webhook:
            return None
        day = datetime.now(ZoneInfo(settings.display_timezone))
        result = await send(webhook, build_text(day, name, lines, ok=ok), transport=transport)
    except Exception:
        logger.exception("작업 결과를 슬랙으로 보내지 못했다: %s", name)
        return None
    logger.info("작업 결과 %s: %s", name, result.detail)
    return result


def report_sync(name: str, lines: Sequence[str], *, ok: bool = True) -> None:
    """스레드에서 도는 작업이 부른다. 이벤트 루프 위에서는 부르지 않는다."""
    if not get_settings().daily_report_slack_webhook:
        return
    try:
        asyncio.run(report(name, lines, ok=ok))
    except Exception:
        logger.exception("작업 결과를 슬랙으로 보내지 못했다: %s", name)
