"""운영 설정 화면의 조각 라우트.

`app/api/settings.py` 를 그대로 부른다. 값 검증은 `app/settings.py` 가 하고, 화면은 거절 사유를
그대로 옮긴다 — 화면에서만 통과하는 값이 생기면 어느 쪽이 진실인지 알 수 없게 된다.

이 화면에 나오는 값은 동시 실행 상한 하나다. 환경변수로 충분한 값을 여기로 옮기면 같은 설정이
두 곳에 생긴다 (`app/settings.py`).

데이터 파일 업로드도 이 화면에 있다. 검증과 병합은 `app/api/import_data.py` 가 하고, 여기서는
올라온 파일을 임시 파일로 받아 넘기고 결과를 숫자로 그린다. 거절 사유는 그대로 옮긴다.
"""

from __future__ import annotations

import logging
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask

from app import db
from app import settings as store
from app.api import import_data
from app.api import settings as settings_api
from app.api.ui import render
from app.api.ui_crawlers import error_detail
from app.api.workflows import get_workflow_scheduler
from app.config import get_settings
from app.crawler import daily
from app.scheduler import DAILY_JOB_ID, WorkflowScheduler

logger = logging.getLogger(__name__)

# 업로드를 읽어 들이는 덩어리 크기. 전체를 메모리에 올리지 않는다
_CHUNK_BYTES = 1024 * 1024

router = APIRouter(tags=["ui"], include_in_schema=False)


def _form(
    request: Request,
    conn: sqlite3.Connection,
    *,
    message: str = "",
    error: dict[str, str] | None = None,
) -> HTMLResponse:
    """설정 폼 하나. 저장 결과도 이 조각으로 돌아온다."""
    return render(
        request,
        "fragments/settings_form.html",
        values=settings_api.read_settings(conn),
        key=store.MAX_CONCURRENT_RUNS,
        message=message,
        error=error,
    )


@router.get("/ui/settings", response_class=HTMLResponse)
def settings_form_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(settings_api.get_connection)],
) -> HTMLResponse:
    """저장된 값. 아직 없는 키는 환경변수 값으로 채워진 뒤 돌아온다."""
    return _form(request, conn)


def _daily_form(
    request: Request,
    conn: sqlite3.Connection,
    scheduler: WorkflowScheduler,
    *,
    message: str = "",
    error: str = "",
) -> HTMLResponse:
    """매일 수집 설정 폼. 지금 사이트 수로 나눈 간격과 다음 시작 시각을 같이 적는다."""
    config = daily.read_config(conn)
    count = len(daily.active_workflows(conn))
    job = scheduler.scheduler.get_job(DAILY_JOB_ID)
    next_at = getattr(job, "next_run_time", None) if job is not None else None
    return render(
        request,
        "fragments/daily_crawl_form.html",
        config=config,
        zone=get_settings().display_timezone,
        max_spread=daily.MAX_SPREAD_MINUTES,
        count=count,
        gap=round(config.spread_minutes / count) if count else 0,
        next_at=next_at,
        message=message,
        error=error,
    )


@router.get("/ui/settings/daily", response_class=HTMLResponse)
def daily_form_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(settings_api.get_connection)],
    scheduler: Annotated[WorkflowScheduler, Depends(get_workflow_scheduler)],
) -> HTMLResponse:
    return _daily_form(request, conn, scheduler)


@router.post("/ui/settings/daily", response_class=HTMLResponse)
def update_daily_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(settings_api.get_connection)],
    scheduler: Annotated[WorkflowScheduler, Depends(get_workflow_scheduler)],
    start_time: Annotated[str, Form()] = "",
    spread_minutes: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """저장하고 매일 잡의 시각을 바로 맞춘다. 거절된 값은 저장되지 않는다."""
    try:
        minutes = int(spread_minutes)
    except ValueError:
        return _daily_form(
            request, conn, scheduler, error=f"걸쳐 도는 시간이 정수가 아니다: {spread_minutes!r}"
        )
    config = daily.DailyConfig(start_time=start_time.strip(), spread_minutes=minutes)
    try:
        daily.write_config(conn, config)
    except daily.DailySettingError as exc:
        return _daily_form(request, conn, scheduler, error=str(exc))
    scheduler.sync(conn)
    return _daily_form(
        request,
        conn,
        scheduler,
        message=f"매일 {config.start_time}부터 {config.spread_minutes}분에 걸쳐 돈다",
    )


@router.put("/ui/settings/{key}", response_class=HTMLResponse)
def update_setting_fragment(
    request: Request,
    key: str,
    conn: Annotated[sqlite3.Connection, Depends(settings_api.get_connection)],
    value: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """값을 바꾼다. 거절된 값은 저장되지 않고 저장된 값이 그대로 다시 그려진다."""
    try:
        parsed = int(value)
    except ValueError:
        # 실패에는 사유와 다음에 할 일이 함께 나온다 (`app/api/ui.py` 의 `NEXT_STEPS`)
        return _form(
            request,
            conn,
            error={"reason": "invalid_input", "message": f"정수가 아니다: {value!r}"},
        )

    try:
        saved = settings_api.update_setting(key, settings_api.SettingUpdate(value=parsed), conn)
    except HTTPException as exc:
        return _form(request, conn, error=error_detail(exc))

    return _form(request, conn, message=f"{saved.key} 를 {saved.value} 로 저장했다")


@router.post("/ui/settings/import", response_class=HTMLResponse)
def import_upload_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(settings_api.get_connection)],
    scheduler: Annotated[WorkflowScheduler, Depends(get_workflow_scheduler)],
    file: Annotated[UploadFile, File()],
) -> HTMLResponse:
    """올린 SQLite 파일을 기존 데이터에 더하고 결과를 숫자로 돌려준다.

    파일은 임시 디렉터리에 받아 두고 끝나면 지운다. SQLite 는 경로가 있어야 열 수 있고, 검증도
    병합도 파일을 여러 번 연다.

    새 워크플로우가 들어왔으면 스케줄러에 알린다. `workflows` 테이블이 진실이고 잡은 그것을
    따라간다 (`.claude/rules/crawling.md`). 다음 기동까지 기다리면 방금 가져온 워크플로우가
    도는지 화면에서 확인할 방법이 없다.
    """
    with tempfile.TemporaryDirectory(prefix="import-") as workspace:
        try:
            path = _spool(file, Path(workspace))
            result = import_data.import_database(conn, path)
        except import_data.ImportRejected as rejected:
            logger.info("데이터 가져오기를 거절했다: %s / %s", rejected.reason, rejected.message)
            return render(request, "fragments/import_result.html", error=rejected)

    if result.workflows_added:
        scheduler.sync(conn)
    return render(request, "fragments/import_result.html", result=result)


def _spool(file: UploadFile, directory: Path) -> Path:
    """올라온 내용을 파일로 받는다. 상한을 넘으면 받다 말고 거절한다.

    다 받은 뒤에 크기를 재지 않는다. 상한이 있는 이유는 디스크를 지키는 것이고, 다 받고 나서
    재면 이미 다 쓴 뒤다.
    """
    path = directory / "upload.db"
    size = 0
    with path.open("wb") as target:
        while chunk := file.file.read(_CHUNK_BYTES):
            size += len(chunk)
            if size > import_data.MAX_UPLOAD_BYTES:
                raise import_data.ImportRejected(
                    "too_large",
                    f"파일이 상한 {import_data.MAX_UPLOAD_BYTES}바이트를 넘는다",
                )
            target.write(chunk)
    return path


@router.get("/ui/settings/export")
def export_snapshot() -> FileResponse:
    """지금 DB 를 파일 하나로 내려받는다.

    `VACUUM INTO` 로 뜬다. 파일을 그대로 복사하면 쓰기 도중의 페이지가 섞여 열리지 않는
    파일이 나온다. 워크플로우가 30분마다 도는 서버에서 그 순간을 피할 방법은 없다.

    받은 파일은 그대로 다른 서버의 `데이터 파일 가져오기` 에 올릴 수 있다. 그쪽이 검증하고
    없는 것만 더한다.
    """
    source = get_settings().database_path
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M")
    target = Path(tempfile.gettempdir()) / f"jobs-{stamp}.db"
    target.unlink(missing_ok=True)

    conn = db.connect(source)
    try:
        # 경로를 문자열로 끼워 넣는다. SQLite 가 이 자리에 바인딩을 받지 않는다.
        # 값은 우리가 만든 임시 경로라 밖에서 오지 않는다
        conn.execute(f"VACUUM INTO '{target}'")
    finally:
        conn.close()

    logger.info("스냅샷 내보내기 %s (%d bytes)", target.name, target.stat().st_size)
    return FileResponse(
        target,
        media_type="application/vnd.sqlite3",
        filename=target.name,
        background=BackgroundTask(target.unlink, missing_ok=True),
    )
