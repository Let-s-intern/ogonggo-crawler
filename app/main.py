"""FastAPI 앱. 라우터 등록과 스케줄러 기동."""

import logging
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import db, industries, taxonomy
from app.api import (
    auth,
    classify,
    crawlers,
    jobs,
    review,
    review_actions,
    review_filter,
    rules,
    settings,
    side,
    ui,
    ui_ai_quick,
    ui_bootcamps,
    ui_companies,
    ui_cost,
    ui_crawlers,
    ui_deliver,
    ui_fields,
    ui_industries,
    ui_llm,
    ui_notify,
    ui_posting_add,
    ui_prompt_rules,
    ui_rules,
    ui_rules_preview,
    ui_runs,
    ui_settings,
    ui_side,
    ui_site_add,
    ui_sites,
    ui_storage,
    ui_taxonomy,
    ui_tests,
    ui_workflows,
    workflows,
)
from app.bootcamp import schedule as bootcamp_schedule
from app.bootcamp.runner import close_orphan_runs as close_orphan_bootcamp_runs
from app.config import get_settings
from app.crawler.fetcher import close_fetcher
from app.crawler.runner import close_orphan_runs
from app.log_ring import handler as _log_ring_handler
from app.scheduler import get_scheduler, shutdown_scheduler
from app.side.runs import close_orphans as close_orphan_side_runs

logger = logging.getLogger(__name__)

# 대시보드의 실시간 로그가 읽는 자리. `app` 로거 아래는 전부 기본이 `NOTSET` 이라 이 한
# 줄이 없으면 부모(루트, 기본 WARNING)를 따라가 `logger.info` 호출이 버퍼에 닿지 않는다.
# 모듈이 다시 임포트돼도(리로드 없는 한 프로세스 안에서는 일어나지 않지만) 핸들러가
# 두 번 붙지 않게 막는다 — 붙으면 로그 줄마다 화면에 두 번씩 나온다.
_app_logger = logging.getLogger("app")
_app_logger.setLevel(logging.INFO)
if _log_ring_handler not in _app_logger.handlers:
    _app_logger.addHandler(_log_ring_handler)


def _seed_empty_tables(conn: sqlite3.Connection) -> None:
    """직무 분류표와 산업 분류표가 비어 있으면 씨앗을 넣는다.

    두 표가 비면 분류가 직군·직무·산업을 묻지 않고 빈 채로 저장한다. 에러도 없어서 새로 띄운
    서버에서 세 칸이 조용히 빈다 (2026-09-18 확인). 불러오기 단추를 누르기 전에 수집이 돌면 그
    공고들은 분류가 끝난 것으로 남아 다시 돌지 않는다. 표에 한 줄이라도 있으면 `load_seed` 가
    아무것도 하지 않는다 — 운영자가 고친 표는 건드리지 않는다.
    """
    majors, minors = taxonomy.load_seed(conn, ui_taxonomy.SEED_PATH)
    if majors:
        logger.info("직무 분류표가 비어 있어 씨앗을 넣었다: 대분류 %d, 소분류 %d", majors, minors)
    added = industries.load_seed(conn, ui_industries.SEED_PATH)
    if added:
        logger.info("산업 분류표가 비어 있어 씨앗을 넣었다: %d개", added)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """기동 시 `workflows` 테이블에서 잡을 등록한다. 스키마 적용은 CLI 가 한다."""
    if auth.admin_password_is_default():
        logger.warning(
            "ADMIN_PASSWORD 가 기본값이다. 공개 주소에서는 잠기지 않은 것과 같다 — "
            "환경변수를 설정하고 다시 띄운다"
        )
    conn = db.connect()
    try:
        orphans = close_orphan_runs(conn)
        if orphans:
            logger.warning("지난 프로세스가 남긴 미완 실행 %d건을 timeout 으로 닫았다", orphans)
        # 부가 워크플로우도 같은 뒷정리가 필요하다. 그리고 여기는 화면 표시만의 문제가
        # 아니다 — 겹침 방지가 "열린 행이 있으면 돌고 있는 것" 으로 판단하므로, 죽은
        # 프로세스가 남긴 행 하나가 그 워크플로우를 영영 막는다 (`app/side/runner.py`)
        side_orphans = close_orphan_side_runs(conn)
        if side_orphans:
            logger.warning(
                "지난 프로세스가 남긴 미완 부가 실행 %d건을 timeout 으로 닫았다", side_orphans
            )
        bootcamp_orphans = close_orphan_bootcamp_runs(conn)
        if bootcamp_orphans:
            logger.warning(
                "지난 프로세스가 남긴 미완 부트캠프 수집 %d건을 닫았다", bootcamp_orphans
            )
        try:
            _seed_empty_tables(conn)
            get_scheduler().start(conn)
            bootcamp_schedule.sync(get_scheduler().scheduler, conn)
        except sqlite3.OperationalError:
            # 스키마가 아직 없는 DB 다. 등록할 워크플로우도 없다.
            #
            # 운영에서는 컨테이너가 uvicorn 앞에서 마이그레이션을 돌리므로 여기 오지 않는다
            # (`Dockerfile` 의 CMD). 여기서 예외를 올리면 스키마가 없다는 이유로 앱이 아예
            # 뜨지 않아, 마이그레이션을 돌릴 화면도 API 도 못 쓰게 된다.
            logger.warning("스키마가 없어 워크플로우를 등록하지 못했다. 마이그레이션이 필요하다")
    finally:
        conn.close()
    try:
        yield
    finally:
        shutdown_scheduler()
        await close_fetcher()


app = FastAPI(title="job-crawler-automation", lifespan=lifespan)
app.include_router(crawlers.router)
app.include_router(jobs.router)
app.include_router(workflows.router)
app.include_router(settings.router)
app.include_router(rules.router)
app.include_router(classify.router)
app.include_router(side.router)
# 화면. API 라우터 뒤에 붙인다 — `/api/...` 가 먼저 잡힌다
app.include_router(ui.router)
app.include_router(ui_cost.router)
app.include_router(ui_fields.router)
app.include_router(ui_crawlers.router)
app.include_router(ui_tests.router)
app.include_router(ui_workflows.router)
app.include_router(ui_sites.router)
app.include_router(ui_site_add.router)
app.include_router(ui_posting_add.router)
app.include_router(ui_runs.router)
app.include_router(ui_rules.router)
app.include_router(ui_rules_preview.router)
app.include_router(review_filter.router)
app.include_router(review.router)
app.include_router(review_actions.router)
app.include_router(ui_companies.router)
app.include_router(ui_side.router)
app.include_router(ui_deliver.router)
app.include_router(ui_settings.router)
app.include_router(ui_notify.router)
app.include_router(ui_storage.router)
app.include_router(ui_llm.router)
app.include_router(ui_ai_quick.router)
app.include_router(ui_taxonomy.router)
app.include_router(ui_industries.router)
app.include_router(ui_prompt_rules.router)
app.include_router(ui_bootcamps.router)
# 조각 요청의 실패는 200 과 오류 조각으로 나간다. HTMX 가 4xx·5xx 를 갈아 끼우지 않아
# 그대로 두면 화면이 조용해진다. `/api/...` 의 상태 코드는 건드리지 않는다
ui.install_ui_error_handlers(app)
# 잠금은 라우트 등록이 끝난 뒤에 건다. 열어 두는 것은 `/health` 와 로그인 자리뿐이고
# 나머지는 전부 잠긴다 — 새 라우트가 생겨도 기본이 잠김이다 (`app/api/auth.py`)
auth.install_auth(app)


@app.get("/health")
def health() -> dict[str, str]:
    """Coolify 가 배포 성공을 판정하는 자리. 어떤 코드가 떠 있는지도 같이 돌려준다.

    이미지 태그를 커밋 SHA 로 고정하지 않으므로, 배포된 것이 무엇인지 아는 길이
    이 값뿐이다. 빌드가 심고(`Dockerfile` 의 `BUILD_SHA`) 사람이 손대지 않는다.
    """
    return {"status": "ok", "build": get_settings().build_sha}
