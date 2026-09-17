"""사이트 추가. 목록 주소와 회사 이름만 받아 등록 → 시험 수집 → 자동 수집 시작까지 창 하나에서 한다.

2026-09-17 결정(LC-3344). 예전에는 크롤러 등록 · 테스트 실행 · 워크플로우 승격이 세 화면에 흩어져
있어 무엇을 순서대로 눌러야 하는지 알 수 없었다.

각 단계는 이미 있는 경로를 그대로 부른다 — 등록은 `crawlers.create_crawler`(목록 주소로 상세 경로를
스스로 찾는 판정 포함), 시험 수집은 `crawlers.test_run`, 시작은 `workflows.promote` 다. 화면 전용
경로를 따로 만들면 화면에서 되는 일이 API 에서 안 되는 상태가 생긴다.

## 못 찾았을 때

실제 사이트 다섯 곳에서 목록 주소만으로 끝까지 된 곳이 없었다 (2026-09-17 측정). 그래서 실패가
드문 경우가 아니라 흔한 경우로 보고, 무엇까지 됐는지와 다음에 할 일을 같은 창에 둔다.

- 공고 하나의 주소로 다시 찾기: 같은 목록 주소와 그 공고 주소로 새로 등록한다. 방금 만든 초안
  크롤러는 지운다 — 초안은 워크플로우가 없어 수집한 공고도 없다
- 셀렉터 직접 고치기: 시험 실행 화면으로 보낸다
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.api import crawlers, workflows
from app.api.ui import render
from app.api.ui_crawlers import error_detail
from app.api.ui_sites import PROBLEMS, UNKNOWN_PROBLEM
from app.crawler.fetcher import FetchPolicy
from app.scheduler import WorkflowScheduler

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ui"], include_in_schema=False)

# 시험 수집에서 상세까지 따라갈 공고 수
TRY_LIMIT = 3

# 고를 수 있는 수집 주기(분)와 이름
INTERVALS: tuple[tuple[int, str], ...] = (
    (30, "30분마다"),
    (60, "1시간마다"),
    (180, "3시간마다"),
    (360, "6시간마다"),
    (1440, "하루에 한 번"),
)

# 등록이 거절한 사유를 쉬운 말로. 없는 사유는 원래 문장을 그대로 쓴다
REGISTER_PROBLEMS: dict[str, str] = {
    "list_not_found": (
        "이 주소에서 공고 목록을 찾지 못했어요. 채용공고 목록이 보이는 페이지 주소인지"
        " 확인해 주세요"
    ),
    "list_fields_not_found": "반복되는 목록은 찾았지만 공고 제목이나 링크를 읽지 못했어요",
    "robots": "사이트가 로봇 수집을 막아 둔 주소예요. 다른 주소를 써야 합니다",
    "transport": "사이트에 접속하지 못했어요. 주소를 확인해 주세요",
    "no_api_key": "AI 키가 없어 셀렉터를 만들 수 없어요. 설정 > AI 에서 키를 넣어 주세요",
    "api_error": "AI 호출이 실패했어요. 잠시 뒤 다시 시도해 주세요",
}


def _form(request: Request, **context: object) -> HTMLResponse:
    return render(request, "fragments/site_add.html", intervals=INTERVALS, **context)


@router.get("/ui/sites/new", response_class=HTMLResponse)
def site_add_form(request: Request) -> HTMLResponse:
    """사이트 추가 창의 첫 화면. 목록 주소와 회사 이름만 받는다."""
    return _form(request, step="form", list_url="", company="")


def _drop_draft(conn: sqlite3.Connection, crawler_id: int) -> None:
    """다시 찾기 전에 방금 만든 초안을 지운다. 워크플로우가 붙은 크롤러는 건드리지 않는다."""
    row = conn.execute(
        "SELECT status FROM crawlers WHERE id = ?"
        " AND NOT EXISTS (SELECT 1 FROM workflows WHERE crawler_id = crawlers.id)",
        (crawler_id,),
    ).fetchone()
    if row is None or row["status"] == "promoted":
        return
    try:
        crawlers.delete_crawler(crawler_id, conn)
    except HTTPException as exc:
        logger.warning("사이트 추가: 초안 크롤러 %s 를 지우지 못했다: %s", crawler_id, exc.detail)


@router.post("/ui/sites/new", response_class=HTMLResponse)
async def site_add_try(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
    generate: Annotated[crawlers.GenerateFn, Depends(crawlers.get_generator)],
    discover: Annotated[crawlers.DiscoverFn, Depends(crawlers.get_discoverer)],
    fetcher: Annotated[FetchPolicy, Depends(crawlers.get_crawl_fetcher)],
    list_url: Annotated[str, Form()] = "",
    company: Annotated[str, Form()] = "",
    detail_url: Annotated[str, Form()] = "",
    replace_crawler_id: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """등록하고 곧바로 공고 몇 건을 시험 수집한다. 결과에 따라 시작 또는 다시 찾기로 이어진다."""
    list_url, company, detail_url = list_url.strip(), company.strip(), detail_url.strip()
    kept = {"list_url": list_url, "company": company, "detail_url": detail_url}
    if not list_url.startswith(("http://", "https://")) or not company:
        return _form(
            request,
            step="form",
            error="목록 페이지 주소(http:// 나 https://)와 회사 이름을 모두 넣어 주세요",
            **kept,
        )
    if detail_url and not detail_url.startswith(("http://", "https://")):
        return _form(
            request,
            step="notfound",
            error="공고 주소는 http:// 나 https:// 로 시작해야 합니다",
            crawler_id=replace_crawler_id,
            **kept,
        )
    if replace_crawler_id.strip().isdigit():
        _drop_draft(conn, int(replace_crawler_id))

    try:
        created = await crawlers.create_crawler(
            crawlers.CrawlerCreate(
                list_url=list_url, detail_url=detail_url, default_company=company
            ),
            conn,
            generate,
            discover,
        )
    except HTTPException as exc:
        detail = error_detail(exc)
        return _form(
            request,
            step="notfound",
            problem=REGISTER_PROBLEMS.get(detail.get("reason", ""), detail.get("message", "")),
            technical=detail.get("message", ""),
            crawler_id="",
            **kept,
        )

    try:
        run = await crawlers.test_run(created.id, conn, fetcher, TRY_LIMIT, "")
    except HTTPException as exc:
        detail = error_detail(exc)
        return _form(
            request,
            step="notfound",
            problem="셀렉터는 만들었지만 시험 수집을 돌리지 못했어요",
            technical=detail.get("message", ""),
            crawler_id=created.id,
            created=created,
            **kept,
        )

    if run.status == "success" and run.success_count > 0:
        return _form(request, step="found", crawler_id=created.id, created=created, run=run, **kept)
    return _form(
        request,
        step="notfound",
        problem=PROBLEMS.get(run.error_class or "", UNKNOWN_PROBLEM),
        technical=run.error_message,
        crawler_id=created.id,
        created=created,
        run=run,
        **kept,
    )


@router.post("/ui/sites/new/{crawler_id}/start", response_class=HTMLResponse)
def site_add_start(
    request: Request,
    crawler_id: int,
    conn: Annotated[sqlite3.Connection, Depends(workflows.get_connection)],
    scheduler: Annotated[WorkflowScheduler, Depends(workflows.get_workflow_scheduler)],
    name: Annotated[str, Form()] = "",
    interval_minutes: Annotated[int, Form()] = 30,
) -> HTMLResponse:
    """시험 수집을 통과한 사이트의 자동 수집을 시작한다."""
    try:
        created = workflows.promote(
            workflows.WorkflowCreate(
                crawler_id=crawler_id, name=name.strip(), interval_minutes=interval_minutes
            ),
            conn,
            scheduler,
        )
    except HTTPException as exc:
        return _form(
            request,
            step="notfound",
            problem="자동 수집을 시작하지 못했어요",
            technical=error_detail(exc).get("message", ""),
            crawler_id=crawler_id,
            list_url="",
            company="",
            detail_url="",
        )
    response = _form(request, step="started", workflow=created)
    # 사이트 목록을 다시 부르게 한다
    response.headers["HX-Trigger"] = "site-added"
    return response
