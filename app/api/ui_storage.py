"""저장소 설정 화면의 조각 라우트.

값 검증은 `app/storage/settings.py` 가 하고 화면은 거절 사유를 그대로 옮긴다. 화면에서만
통과하는 값이 생기면 어느 쪽이 진실인지 알 수 없게 된다 (`app/api/ui_notify.py` 와 같은
규칙이다).

주소를 `/ui/settings/...` 아래 두지 않는다. `app/api/ui_settings.py` 에 이미
`PUT /ui/settings/{key}` 가 있어서, 같은 자리에 두면 키 이름이 `storage` 인 정수 설정으로
먼저 잡힌다.

**연결 확인은 저장된 설정으로 한다.** 폼에 적힌 값이 아니라 다음 업로드가 실제로 쓰게 될
값이라야 확인이 뜻을 갖는다. 그래서 순서는 저장하고 나서 확인이다.

비밀 키를 비워 두고 저장하면 저장된 값이 그대로 남는다. 여섯이 한 벌로 저장되므로, 비움을
지움으로 읽으면 버킷 이름 하나 고치려다 키가 날아간다 (`app/api/ui_llm.py` 는 키 하나씩
저장해서 비움이 지움이다 — 저장 단위가 다르다).
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.api.settings import get_connection
from app.api.ui import render
from app.storage import s3
from app.storage import settings as store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ui"], include_in_schema=False)


def _form(
    request: Request,
    conn: sqlite3.Connection,
    *,
    message: str = "",
    error: dict[str, str] | None = None,
    checked: s3.CheckResult | None = None,
) -> HTMLResponse:
    """저장소 설정 폼 하나. 저장 결과도 연결 확인 결과도 이 조각으로 돌아온다."""
    config = store.read_config(conn)
    return render(
        request,
        "fragments/storage_form.html",
        config=config,
        # 화면에서 저장한 값이 없고 환경변수로 도는 중인지. 폼에 무엇이 어디서 왔는지 적는다
        from_env=not store.stored_keys(conn) and store.env_config() is not None,
        secret_tail=store.mask(config.secret_key),
        accepted=s3.ACCEPTED,
        max_label=s3.MAX_IMAGE_LABEL,
        message=message,
        error=error,
        checked=checked,
        checked_at=datetime.now(UTC) if checked is not None else None,
    )


@router.get("/ui/storage", response_class=HTMLResponse)
def storage_form_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> HTMLResponse:
    """저장된 설정. 아직 저장한 적이 없으면 기본값이 그려진다."""
    return _form(request, conn)


@router.put("/ui/storage", response_class=HTMLResponse)
def update_storage_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    endpoint: Annotated[str, Form()] = "",
    region: Annotated[str, Form()] = "",
    bucket: Annotated[str, Form()] = "",
    access_key: Annotated[str, Form()] = "",
    secret_key: Annotated[str, Form()] = "",
    public_base: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """설정 한 벌을 저장한다. 하나라도 거절되면 아무것도 저장되지 않는다."""
    stored = store.read_config(conn)
    config = store.StorageConfig(
        endpoint=endpoint.strip(),
        region=region.strip(),
        bucket=bucket.strip(),
        access_key=access_key.strip(),
        # 비우고 저장하면 저장된 값을 그대로 둔다. 화면에 그렇게 적혀 있다
        secret_key=secret_key.strip() or stored.secret_key,
        public_base=public_base.strip(),
    )
    try:
        saved = store.write_config(conn, config)
    except store.StorageSettingError as exc:
        return _form(request, conn, error={"reason": "invalid_value", "message": str(exc)})

    where = saved.endpoint or f"{saved.region} 지역의 S3"
    return _form(
        request,
        conn,
        message=f"저장했다. 다음 업로드는 {where} 의 버킷 `{saved.bucket}` 으로 간다",
    )


@router.post("/ui/storage/check", response_class=HTMLResponse)
def check_storage_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> HTMLResponse:
    """저장된 설정으로 왕복을 한 번 한다. 작은 객체를 넣고, 읽고, 지운다."""
    result = s3.check(store.read_config(conn))
    logger.info("저장소 연결 확인: ok=%s %s에서 %s", result.ok, result.step, result.reason)
    return _form(request, conn, checked=result)
