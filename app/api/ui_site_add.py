"""사이트 추가. 목록 주소와 회사 이름, 수집 주기만 받고 나머지는 요청 밖에서 돈다.

2026-09-17 결정(LC-3344). 예전에는 크롤러 등록 · 테스트 실행 · 워크플로우 승격이 세 화면에 흩어져
있어 무엇을 순서대로 눌러야 하는지 알 수 없었다. 한 창으로 모은 뒤에도 등록과 시험 수집을 창에서
기다려야 해서 한 번에 한 곳밖에 넣지 못했다. 이제 누르면 곧바로 돌아오고, 시험 수집이 되면 자동
수집까지 시작한다. 진행과 실패는 사이트 목록 맨 위에 보인다 (`app/api/site_adds.py`).

각 단계는 이미 있는 경로를 그대로 부른다 — 등록은 `crawlers.create_crawler`(목록 주소로 상세 경로를
스스로 찾는 판정 포함), 시험 수집은 `crawlers.test_run`, 시작은 `workflows.promote` 다. 화면 전용
경로를 따로 만들면 화면에서 되는 일이 API 에서 안 되는 상태가 생긴다.

## 못 찾았을 때

목록 줄의 `다시 찾기` 가 이 창을 다시 연다. 무엇이 안 됐는지와 두 갈래를 둔다.

- 예시 공고 주소로 다시 찾기: 같은 목록 주소와 그 공고 주소로 새로 건다. 방금 만든 초안
  크롤러는 지운다 — 초안은 워크플로우가 없어 수집한 공고도 없다
- 셀렉터 직접 고치기: 시험 실행 화면으로 보낸다
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app import db
from app.api import crawlers, site_adds, workflows
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
DEFAULT_INTERVAL = 60

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
    """사이트 추가 창의 첫 화면. 목록 주소와 회사 이름, 수집 주기만 받는다."""
    return _form(request, step="form", list_url="", company="", interval=DEFAULT_INTERVAL)


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


def get_add_launcher() -> Callable[[Coroutine[Any, Any, None]], None]:
    """사이트 추가를 요청 밖에서 돌린다. 테스트는 이 의존성을 갈아끼운다."""

    def launch(coro: Coroutine[Any, Any, None]) -> None:
        site_adds.keep(asyncio.get_running_loop().create_task(coro))

    return launch


def get_add_connect() -> Callable[[], sqlite3.Connection]:
    """요청 밖 작업이 쓸 연결을 연다. 요청의 연결은 응답과 함께 닫힌다."""
    return db.connect


def _fail(add: site_adds.SiteAdd, problem: str, technical: str) -> None:
    add.state = site_adds.FAILED
    add.problem = problem
    add.technical = technical
    logger.info("사이트 추가 실패: %s %s — %s", add.company, add.list_url, technical or problem)


# 단계 기록에 적는 칸 이름. 없는 칸은 원래 이름 그대로 적는다
FIELD_WORDS: dict[str, str] = {
    "list.item": "목록 항목",
    "list.title": "목록 제목",
    "list.link": "목록 링크",
    "list.date": "목록 날짜",
    "list.company_name": "목록 회사",
    "detail.title": "상세 제목",
    "detail.body": "상세 본문",
}
MODE_WORDS: dict[str, str] = {
    "static": "페이지를 바로 읽음",
    "playwright": "브라우저로 열어 읽음",
    "api": "사이트의 데이터 주소로 읽음",
}


def _matches_line(matches: dict[str, Any], failed: list[str]) -> str:
    """셀렉터가 잡은 수. 목록 칸과 상세 제목·본문, 그리고 실패한 칸만 적는다."""
    shown = [
        f"{FIELD_WORDS.get(name, name)} {count}개"
        for name, count in matches.items()
        if name in FIELD_WORDS or name in failed
    ]
    return "셀렉터가 잡은 수: " + ", ".join(shown) if shown else ""


def _register_failed(detail: dict[str, Any]) -> site_adds.Step:
    lines = [str(detail.get("message", ""))]
    matches = detail.get("matches")
    if isinstance(matches, dict):
        lines.append(_matches_line(matches, list(detail.get("failed_fields") or [])))
    lines.extend(str(note) for note in detail.get("notes") or [])
    return site_adds.Step("목록을 읽고 셀렉터 만들기", False, [line for line in lines if line])


def _registered(created: crawlers.CrawlerOut, detail_url_given: bool) -> list[site_adds.Step]:
    lines = [
        f"목록: {MODE_WORDS.get(created.list_mode, created.list_mode)}",
        _matches_line(created.matches, created.failed_fields),
    ]
    if created.failed_fields:
        words = ", ".join(FIELD_WORDS.get(name, name) for name in created.failed_fields)
        lines.append(f"못 잡은 칸: {words}")
    lines.extend(created.notes)
    selectors = [site_adds.Step("목록을 읽고 셀렉터 만들기", True, [x for x in lines if x])]

    path = [
        f"상세: {MODE_WORDS.get(created.detail_mode, created.detail_mode)}",
        f"상세 페이지 예: {created.detail_url}" if created.detail_url else "",
        "공고 주소를 직접 넣었다" if detail_url_given else "",
        created.path_evidence,
        created.path_reason,
        created.path_failure,
    ]
    found = bool(created.detail_url) and not created.path_failure
    selectors.append(site_adds.Step("상세 페이지로 가는 길 찾기", found, [x for x in path if x]))
    return selectors


def _tested(run: crawlers.TestRunOut) -> site_adds.Step:
    lines = [
        f"목록에서 {run.matched}건, 상세까지 가져온 공고 {run.success_count}건, "
        f"실패 {run.fail_count}건, 건너뜀 {run.skipped_count}건",
    ]
    if run.error_message:
        lines.append(run.error_message)
    for item in run.items[:TRY_LIMIT]:
        body = item.fields.get("body") or ""
        lines.append(f"가져옴: {item.fields.get('title') or '제목 없음'} (본문 {len(body)}자)")
    for failure in run.failures[:5]:
        lines.append(f"실패: {failure.source_url} — {failure.message}")
    ok = run.status == "success" and run.success_count > 0
    return site_adds.Step("공고 몇 건 시험 수집", ok, lines)


async def _work(
    add: site_adds.SiteAdd,
    connect: Callable[[], sqlite3.Connection],
    generate: crawlers.GenerateFn,
    discover: crawlers.DiscoverFn,
    fetcher: FetchPolicy,
    scheduler: WorkflowScheduler,
) -> None:
    """등록 → 시험 수집 → 자동 수집 시작. 단계마다 본 것과 어디서 멈췄는지를 `add` 에 남긴다."""
    async with site_adds.slot():
        conn = connect()
        try:
            add.state = site_adds.FINDING
            try:
                created = await crawlers.create_crawler(
                    crawlers.CrawlerCreate(
                        list_url=add.list_url,
                        detail_url=add.detail_url,
                        default_company=add.company,
                    ),
                    conn,
                    generate,
                    discover,
                )
            except HTTPException as exc:
                detail = error_detail(exc)
                add.steps.append(_register_failed(detail))
                _fail(
                    add,
                    REGISTER_PROBLEMS.get(detail.get("reason", ""), detail.get("message", "")),
                    detail.get("message", ""),
                )
                return

            add.crawler_id = created.id
            add.steps.extend(_registered(created, bool(add.detail_url)))
            add.state = site_adds.TESTING
            try:
                run = await crawlers.test_run(created.id, conn, fetcher, TRY_LIMIT, "")
            except HTTPException as exc:
                message = error_detail(exc).get("message", "")
                add.steps.append(site_adds.Step("공고 몇 건 시험 수집", False, [message]))
                _fail(add, "셀렉터는 만들었지만 시험 수집을 돌리지 못했어요", message)
                return
            add.matched, add.success_count = run.matched, run.success_count
            add.steps.append(_tested(run))
            if run.status != "success" or run.success_count == 0:
                _fail(add, PROBLEMS.get(run.error_class or "", UNKNOWN_PROBLEM), run.error_message)
                return

            try:
                workflows.promote(
                    workflows.WorkflowCreate(
                        crawler_id=created.id,
                        name=add.company,
                        interval_minutes=add.interval_minutes,
                    ),
                    conn,
                    scheduler,
                )
            except HTTPException as exc:
                message = error_detail(exc).get("message", "")
                add.steps.append(site_adds.Step("자동 수집 시작", False, [message]))
                _fail(add, "자동 수집을 시작하지 못했어요", message)
                return
            # 사이트가 됐다. 이제 사이트 목록의 한 줄로 보인다
            site_adds.forget(add.id)
            logger.info("사이트 추가: %s 자동 수집을 시작했다", add.company)
        except Exception as exc:  # noqa: BLE001 — 요청 밖이라 여기서 잡지 않으면 아무도 모른다
            logger.exception("사이트 추가: %s 에서 예상하지 못한 오류", add.list_url)
            add.steps.append(
                site_adds.Step("예상하지 못한 오류", False, [f"{type(exc).__name__}: {exc}"])
            )
            _fail(add, UNKNOWN_PROBLEM, f"{type(exc).__name__}: {exc}")
        finally:
            conn.close()


@router.post("/ui/sites/new", response_class=HTMLResponse)
async def site_add_try(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
    generate: Annotated[crawlers.GenerateFn, Depends(crawlers.get_generator)],
    discover: Annotated[crawlers.DiscoverFn, Depends(crawlers.get_discoverer)],
    fetcher: Annotated[FetchPolicy, Depends(crawlers.get_crawl_fetcher)],
    scheduler: Annotated[WorkflowScheduler, Depends(workflows.get_workflow_scheduler)],
    launch: Annotated[Callable[[Coroutine[Any, Any, None]], None], Depends(get_add_launcher)],
    connect: Annotated[Callable[[], sqlite3.Connection], Depends(get_add_connect)],
    list_url: Annotated[str, Form()] = "",
    company: Annotated[str, Form()] = "",
    detail_url: Annotated[str, Form()] = "",
    interval_minutes: Annotated[int, Form()] = DEFAULT_INTERVAL,
    replace_add_id: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """사이트 추가를 걸고 곧바로 돌아온다. 창을 닫거나 다른 곳을 또 걸어도 된다."""
    list_url, company, detail_url = list_url.strip(), company.strip(), detail_url.strip()
    kept = {
        "list_url": list_url,
        "company": company,
        "detail_url": detail_url,
        "interval": interval_minutes,
    }
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
            step="form",
            error="공고 주소는 http:// 나 https:// 로 시작해야 합니다",
            **kept,
        )
    if replace_add_id.strip().isdigit():
        old = site_adds.forget(int(replace_add_id))
        if old is not None and old.crawler_id is not None:
            _drop_draft(conn, old.crawler_id)

    allowed = {minutes for minutes, _ in INTERVALS}
    add = site_adds.new(
        list_url,
        company,
        detail_url,
        interval_minutes if interval_minutes in allowed else DEFAULT_INTERVAL,
    )
    launch(_work(add, connect, generate, discover, fetcher, scheduler))
    response = _form(request, step="queued", add=add)
    response.headers["HX-Trigger"] = "site-added"
    return response


@router.get("/ui/sites/new/{add_id}", response_class=HTMLResponse)
def site_add_retry_form(request: Request, add_id: int) -> HTMLResponse:
    """찾지 못한 사이트 추가를 다시 여는 창. 무엇이 안 됐는지와 공고 주소로 다시 찾기를 둔다."""
    add = site_adds.get(add_id)
    if add is None:
        return _form(
            request,
            step="form",
            error="그 사이트 추가 기록이 없어요. 서버가 다시 떴을 수 있어요. 처음부터 넣어 주세요",
            list_url="",
            company="",
            interval=DEFAULT_INTERVAL,
        )
    return _form(
        request,
        step="notfound",
        add=add,
        list_url=add.list_url,
        company=add.company,
        detail_url=add.detail_url,
        interval=add.interval_minutes,
    )


@router.post("/ui/sites/new/{add_id}/dismiss", response_class=HTMLResponse)
def site_add_dismiss(
    add_id: int,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    """찾지 못한 사이트 추가를 목록에서 지운다. 만들다 만 초안도 같이 지운다."""
    add = site_adds.get(add_id)
    if add is not None and not add.running:
        site_adds.forget(add_id)
        if add.crawler_id is not None:
            _drop_draft(conn, add.crawler_id)
    return HTMLResponse("", headers={"HX-Trigger": "site-added"})
