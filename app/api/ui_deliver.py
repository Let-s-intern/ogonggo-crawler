"""오공고(Spring) 전송 설정 화면의 조각 라우트.

값 검증은 `app/deliver/settings.py` 가 하고 화면은 거절 사유를 그대로 옮긴다
(`app/api/ui_notify.py` 와 같은 규칙). 보내는 일은 `app/deliver/spring.py` 가 한다.

키는 화면에서 받지 않는다. 크롤러 `.env` 의 `OGONGGO_INTERNAL_API_KEY` 가 있는지만 보인다
(2026-09-15 결정).
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.api.settings import get_connection
from app.api.ui import render
from app.config import Settings, get_settings
from app.deliver import settings as store
from app.deliver import spring

router = APIRouter(tags=["ui"], include_in_schema=False)


def _form(
    request: Request,
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    message: str = "",
    error: dict[str, str] | None = None,
) -> HTMLResponse:
    """전송 설정 폼과 보낸 기록 하나."""
    return render(
        request,
        "fragments/deliver_form.html",
        config=store.read_config(conn),
        key_configured=bool(settings.ogonggo_internal_api_key.strip()),
        overview=spring.overview(conn),
        max_attempts=spring.MAX_ATTEMPTS,
        message=message,
        error=error,
    )


@router.get("/ui/deliver", response_class=HTMLResponse)
def deliver_form_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """저장된 설정. 아직 저장한 적이 없으면 기본값이 그려진다."""
    return _form(request, conn, settings)


@router.put("/ui/deliver", response_class=HTMLResponse)
def update_deliver_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
    url: Annotated[str, Form()] = "",
    enabled: Annotated[str, Form()] = "",
    batch_size: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """설정 한 벌을 저장한다. 하나라도 거절되면 아무것도 저장되지 않는다."""
    try:
        size = int(batch_size) if batch_size.strip() else store.DEFAULT_BATCH_SIZE
    except ValueError:
        return _form(
            request,
            conn,
            settings,
            error={
                "reason": "invalid_input",
                "message": f"한 번에 보내는 건수가 정수가 아니다: {batch_size!r}",
            },
        )
    config = store.DeliverConfig(url=url.strip(), enabled=enabled == "1", batch_size=size)
    try:
        saved = store.write_config(conn, config)
    except store.DeliverSettingError as exc:
        return _form(
            request, conn, settings, error={"reason": "invalid_value", "message": str(exc)}
        )
    word = "켜졌다" if saved.enabled else "꺼져 있다"
    return _form(request, conn, settings, message=f"저장했다. 분류 뒤 전송은 {word}")


@router.post("/ui/deliver/send", response_class=HTMLResponse)
async def send_now_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """저장된 설정으로 한 번 보낸다. 켜기·끄기와 상관없이 보낸다."""
    result = await spring.deliver_pending(conn, settings=settings, force=True)
    if result.reason:
        return _form(
            request, conn, settings, error={"reason": "not_sent", "message": result.reason}
        )
    if not result.sent and not result.failed:
        return _form(request, conn, settings, message="보낼 공고가 없다")
    return _form(
        request,
        conn,
        settings,
        message=f"오공고에 {result.sent}건 등록했다. 실패 {result.failed}건",
    )
