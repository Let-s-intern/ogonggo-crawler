"""사이트 화면. 워크플로우 하나가 사이트 한 줄이고, 누르면 오른쪽 패널에서 보고 고친다.

2026-09-17 결정(LC-3344). 예전 워크플로우 화면은 카드마다 버튼이 아홉이라 무엇을 눌러야 할지
알 수 없었다. 목록은 상태·새 공고·마지막 수집만 한 줄로 보이고, 조작은 패널로 옮겼다.

패널의 조작은 새로 만들지 않는다. 지금 수집·멈추기·주기·임계치·원문 다시 수집은 워크플로우 카드
(`fragments/workflow_card.html`, `app/api/ui_workflows.py`)를 그대로 패널에 넣고, 고치기는 시험
실행과 AI 수정(`app/api/ui_tests.py`)을 그대로 부른다. 화면 전용 경로가 갈라지면 화면에서 되는 일이
스케줄러에서 안 되는 상태가 생긴다.

## 주소 고치기

패널 맨 위에 목록 주소와 예시 공고 주소를 저장된 값으로 채워 보이고, 고쳐 저장할 수 있다 (2026-09-18
결정). 예전에는 등록할 때 넣은 예시 공고 주소를 다시 볼 곳이 없었고 목록 주소는 고칠 수 없었다.
저장만 한다 — 셀렉터를 다시 만드는 것은 아래 "다시 찾기" 다.

## 예시 공고 주소로 다시 찾기

목록에서 상세로 가는 길을 못 찾는 사이트가 있다(`detail_unreachable`). 운영자가 사이트에서 공고
하나를 열어 그 주소를 주면, 목록 주소와 그 주소로 셀렉터를 다시 만든다 — 등록할 때 상세 URL 을
넣는 것과 같은 생성 경로다(`app/api/crawlers.py` 의 `get_generator`). 만든 셀렉터는 저장하지 않고
편집기에 올린다. 시험해 보고 저장하는 것은 운영자다 (`.claude/rules/llm.md` 의 "모델은 제안자").

## 로고

패널에서도 회사 로고 파일을 올린다 (2026-09-29 결정). 사이트 추가 창과 같은 길이다
(`app/api/ui_companies.py` 의 `save_logo_file`). 로고는 크롤러의 회사 이름
(`crawlers.default_company`) 행에 적고, 그 이름은 이 사이트 공고의 모회사로 들어가므로 앞으로
수집하는 공고는 이 로고로 나간다. 이미 쌓인 공고도 다시 정규화해 로고를 바꾼다.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse

from app import companies
from app.api import crawlers, site_adds, workflows
from app.api.ui import mode_word, render
from app.api.ui_companies import save_logo_file
from app.api.ui_crawlers import error_detail
from app.api.ui_tests import repair_panel
from app.api.ui_workflows import TRIGGER_WORDS, UNKNOWN_TRIGGER, CardView, _last_run, _view
from app.crawler.playwright import PLAYWRIGHT
from app.normalize.backfill import rewrite_one
from app.normalize.engine import NormalizeError, RawJobMissingError, load_rules
from app.scheduler import WorkflowScheduler
from app.storage import s3
from app.storage import settings as store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ui"], include_in_schema=False)

# 실패 분류를 운영자가 읽는 한 문장으로. 저장값과 원래 사유는 패널의 기술 정보에 그대로 남는다
PROBLEMS: dict[str, str] = {
    "transport": "사이트에 접속하지 못했어요. 사이트가 잠시 멈췄거나 주소가 바뀌었을 수 있습니다",
    "selector_miss": (
        "페이지는 열었지만 공고 목록을 찾지 못했어요. 사이트 화면 구조가 바뀐 것 같습니다"
    ),
    "list_empty": "공고 목록에서 쓸 수 있는 공고를 하나도 읽지 못했어요",
    "parse": "공고는 찾았지만 제목이나 본문 같은 값을 읽지 못했어요",
    "detail_unreachable": "목록에서 공고는 찾았지만, 공고를 눌러 상세 페이지로 들어가지 못했어요",
    "detail_empty": "상세 페이지는 열었지만 본문이 비어 있었어요",
    "timeout": "수집이 제한 시간 안에 끝나지 않았어요",
}
UNKNOWN_PROBLEM = "수집이 실패했어요. 아래 기술 정보에 원래 사유가 있습니다"

# 패널의 최근 수집 표에 몇 줄까지
RECENT_RUNS = 10


def _problem(conn: sqlite3.Connection, card: CardView) -> str:
    """마지막 실행이 실패했으면 그 이유를 쉬운 말로. 성공이면 빈 문자열이다."""
    row = _last_run(conn, card.item.id)
    if row is None or row["status"] == "success":
        return ""
    if row["status"] == "timeout":
        return PROBLEMS["timeout"]
    return PROBLEMS.get(str(row["error_class"] or ""), UNKNOWN_PROBLEM)


def _cards(conn: sqlite3.Connection, scheduler: WorkflowScheduler) -> list[CardView]:
    next_runs = scheduler.next_run_times(conn)
    return [
        _view(conn, item, next_run_at=next_runs.get(item.id))
        for item in workflows.list_workflows(conn)
    ]


@router.get("/ui/sites", response_class=HTMLResponse)
def site_list_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(workflows.get_connection)],
    scheduler: Annotated[WorkflowScheduler, Depends(workflows.get_workflow_scheduler)],
) -> HTMLResponse:
    """사이트 목록. 문제 있는 사이트가 위로 온다."""
    cards = _cards(conn, scheduler)
    order = {"bad": 0, "warn": 1}
    cards.sort(key=lambda card: (order.get(card.tone, 2), card.item.status != "active"))
    return render(
        request,
        "fragments/site_list.html",
        rows=[(card, _problem(conn, card)) for card in cards],
        adds=site_adds.listed(),
    )


def _recent_runs(conn: sqlite3.Connection, workflow_id: int) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT id, started_at, status, new_count, fail_count, error_class, trigger
          FROM crawl_runs WHERE workflow_id = ? ORDER BY id DESC LIMIT ?
        """,
        (workflow_id, RECENT_RUNS),
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "started_at": str(row["started_at"]),
            "status": row["status"],
            "new": int(row["new_count"]),
            "failed": int(row["fail_count"]),
            "problem": (
                PROBLEMS.get(str(row["error_class"] or ""), "")
                if row["status"] not in (None, "success")
                else ""
            ),
            "trigger": TRIGGER_WORDS.get(str(row["trigger"] or ""), UNKNOWN_TRIGGER),
        }
        for row in rows
    ]


@router.get("/ui/sites/{workflow_id}/panel", response_class=HTMLResponse)
def site_panel_fragment(
    request: Request,
    workflow_id: int,
    conn: Annotated[sqlite3.Connection, Depends(workflows.get_connection)],
    scheduler: Annotated[WorkflowScheduler, Depends(workflows.get_workflow_scheduler)],
) -> HTMLResponse:
    """사이트 하나의 오른쪽 패널. 상태·조작(워크플로우 카드)·최근 수집·고치기가 있다."""
    card = next((card for card in _cards(conn, scheduler) if card.item.id == workflow_id), None)
    if card is None:
        return render(request, "fragments/site_panel.html", card=None, workflow_id=workflow_id)
    return _panel(request, conn, card, workflow_id)


def _panel(
    request: Request,
    conn: sqlite3.Connection,
    card: CardView,
    workflow_id: int,
    *,
    message: str = "",
    error: str = "",
    logo_message: str = "",
    logo_error: str = "",
    company_message: str = "",
) -> HTMLResponse:
    row = conn.execute(
        "SELECT detail_url, default_company, has_affiliates FROM crawlers WHERE id = ?",
        (card.item.crawler_id,),
    ).fetchone()
    company = str(row["default_company"] or "").strip() if row else ""
    saved = companies.read(conn, company) if company else None
    return render(
        request,
        "fragments/site_panel.html",
        card=card,
        workflow_id=workflow_id,
        problem=_problem(conn, card),
        runs=_recent_runs(conn, workflow_id),
        detail_url=str(row["detail_url"] or "") if row else "",
        url_message=message,
        url_error=error,
        company=company,
        has_affiliates=bool(row["has_affiliates"]) if row else False,
        company_message=company_message,
        logo_url=saved.logo_url if saved else None,
        logo_message=logo_message,
        logo_error=logo_error,
        storage_ready=store.read_config(conn).configured,
        accept_attr=s3.ACCEPT_ATTR,
        accepted=s3.ACCEPTED,
        max_label=s3.MAX_IMAGE_LABEL,
    )


def _is_http(url: str) -> bool:
    return url.startswith(("http://", "https://"))


@router.post("/ui/sites/{workflow_id}/urls", response_class=HTMLResponse)
def urls_fragment(
    request: Request,
    workflow_id: int,
    conn: Annotated[sqlite3.Connection, Depends(workflows.get_connection)],
    scheduler: Annotated[WorkflowScheduler, Depends(workflows.get_workflow_scheduler)],
    list_url: Annotated[str, Form()] = "",
    detail_url: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """목록 주소와 예시 공고 주소를 저장한다. 셀렉터는 건드리지 않는다."""
    card = next((card for card in _cards(conn, scheduler) if card.item.id == workflow_id), None)
    if card is None:
        return render(request, "fragments/site_panel.html", card=None, workflow_id=workflow_id)
    list_url, detail_url = list_url.strip(), detail_url.strip()
    if not _is_http(list_url):
        return _panel(
            request,
            conn,
            card,
            workflow_id,
            error="목록 주소는 http:// 나 https:// 로 시작해야 해요",
        )
    if detail_url and not _is_http(detail_url):
        return _panel(
            request,
            conn,
            card,
            workflow_id,
            error="예시 공고 주소는 비우거나 http:// 나 https:// 로 시작해야 해요",
        )
    conn.execute(
        "UPDATE crawlers SET list_url = ?, detail_url = ? WHERE id = ?",
        (list_url, detail_url or None, card.item.crawler_id),
    )
    conn.commit()
    logger.info("사이트 %s: 주소를 고쳤다 list=%s detail=%s", workflow_id, list_url, detail_url)
    card = next(card for card in _cards(conn, scheduler) if card.item.id == workflow_id)
    return _panel(request, conn, card, workflow_id, message="주소를 저장했어요")


@router.post("/ui/sites/{workflow_id}/affiliates", response_class=HTMLResponse)
def affiliates_fragment(
    request: Request,
    workflow_id: int,
    conn: Annotated[sqlite3.Connection, Depends(workflows.get_connection)],
    scheduler: Annotated[WorkflowScheduler, Depends(workflows.get_workflow_scheduler)],
    has_affiliates: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """그룹 채용 사이트인지를 저장하고, 이미 모은 공고의 회사명을 다시 정한다 (0048).

    끄면 이 사이트 공고의 회사명은 사이트 이름으로 고정되고, 켜면 공고에서 읽은 계열사가 회사명이
    된다 (`app/normalize/engine.py` 의 `settle_company`). 이미 오공고로 보낸 공고는 다시 보내지
    않는다.
    """
    card = next((card for card in _cards(conn, scheduler) if card.item.id == workflow_id), None)
    if card is None:
        return render(request, "fragments/site_panel.html", card=None, workflow_id=workflow_id)
    on = bool(has_affiliates)
    conn.execute(
        "UPDATE crawlers SET has_affiliates = ? WHERE id = ?",
        (1 if on else 0, card.item.crawler_id),
    )
    conn.commit()
    done, failed = _renormalize_site(conn, card.item.crawler_id)
    conn.commit()
    logger.info("사이트 %s: 계열사 있음을 %s 로 바꿨다. 공고 %s건", workflow_id, on, done)
    message = (
        "그룹 채용 사이트로 바꿨어요. 공고에서 읽은 계열사가 회사명이 됩니다"
        if on
        else "회사가 하나인 사이트로 바꿨어요. 회사명은 사이트의 회사 이름으로 고정됩니다"
    )
    if done or failed:
        message += f". 이미 모은 공고 {done}건을 다시 정리했어요"
    if failed:
        message += f" — {failed}건은 실패해 그대로예요"
    return _panel(request, conn, card, workflow_id, company_message=message)


def _renormalize_site(conn: sqlite3.Connection, crawler_id: int) -> tuple[int, int]:
    """그 사이트가 모은 공고를 다시 정규화한다. (된 건수, 실패 건수). 한 건이 실패해도 계속한다."""
    raw_job_ids = [
        int(row[0])
        for row in conn.execute(
            "SELECT r.id FROM raw_jobs r JOIN workflows w ON w.id = r.workflow_id"
            " WHERE w.crawler_id = ? ORDER BY r.id",
            (crawler_id,),
        )
    ]
    if not raw_job_ids:
        return 0, 0
    try:
        rules = load_rules(conn)
    except NormalizeError as exc:
        logger.warning("사이트 공고를 다시 정규화하려는데 규칙을 읽지 못했다: %s", exc)
        return 0, len(raw_job_ids)
    failed = 0
    for raw_job_id in raw_job_ids:
        try:
            rewrite_one(conn, raw_job_id, rules)
        except (NormalizeError, RawJobMissingError) as exc:
            logger.warning("사이트 공고 재정규화 실패 raw_jobs %s: %s", raw_job_id, exc)
            failed += 1
    return len(raw_job_ids) - failed, failed


@router.post("/ui/sites/{workflow_id}/logo", response_class=HTMLResponse)
def logo_fragment(
    request: Request,
    workflow_id: int,
    conn: Annotated[sqlite3.Connection, Depends(workflows.get_connection)],
    scheduler: Annotated[WorkflowScheduler, Depends(workflows.get_workflow_scheduler)],
    logo: Annotated[UploadFile | None, File()] = None,
) -> HTMLResponse:
    """이 사이트 회사의 로고 파일을 올린다. 다음 수집부터 이 사이트 공고의 로고가 된다."""
    card = next((card for card in _cards(conn, scheduler) if card.item.id == workflow_id), None)
    if card is None:
        return render(request, "fragments/site_panel.html", card=None, workflow_id=workflow_id)
    row = conn.execute(
        "SELECT default_company FROM crawlers WHERE id = ?", (card.item.crawler_id,)
    ).fetchone()
    company = str(row["default_company"] or "").strip() if row else ""
    if not company:
        return _panel(
            request,
            conn,
            card,
            workflow_id,
            logo_error="이 사이트에 회사 이름이 없어 로고를 붙일 곳이 없어요",
        )
    try:
        public_url = save_logo_file(conn, company, logo)
    except s3.StorageError as exc:
        logger.info("사이트 %s: 로고를 못 올렸다: %s / %s", workflow_id, exc.reason, exc.message)
        return _panel(
            request,
            conn,
            card,
            workflow_id,
            logo_error=f"로고를 올리지 못했어요. 다시 골라 주세요: {exc.message}",
        )
    if not public_url:
        return _panel(request, conn, card, workflow_id, logo_error="올릴 로고 파일을 골라 주세요")
    return _panel(
        request,
        conn,
        card,
        workflow_id,
        logo_message="로고를 바꿨어요. 이 회사 공고는 이제 이 로고로 나갑니다",
    )


@router.post("/ui/sites/{workflow_id}/detail-url", response_class=HTMLResponse)
async def detail_url_fragment(
    request: Request,
    workflow_id: int,
    conn: Annotated[sqlite3.Connection, Depends(workflows.get_connection)],
    detail_url: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """예시 공고 주소로 셀렉터를 다시 만든다. 셀렉터는 저장하지 않고 편집기에 올린다.

    넣은 주소는 예시 공고 주소로 저장한다 — 다음에 패널을 열었을 때 무엇을 넣었는지 보인다.
    """
    row = conn.execute(
        "SELECT w.crawler_id, c.list_url, c.list_mode FROM workflows w"
        " JOIN crawlers c ON c.id = w.crawler_id WHERE w.id = ?",
        (workflow_id,),
    ).fetchone()
    if row is None:
        return render(
            request,
            "fragments/test_repair.html",
            error={"reason": "not_found", "message": f"사이트 {workflow_id} 가 없다"},
        )
    crawler_id = int(row["crawler_id"])
    url = detail_url.strip()
    if not url.startswith(("http://", "https://")):
        return repair_panel(
            request,
            conn,
            crawler_id,
            error={
                "reason": "invalid_input",
                "message": "공고 주소는 http:// 나 https:// 로 시작해야 한다",
            },
        )
    conn.execute("UPDATE crawlers SET detail_url = ? WHERE id = ?", (url, crawler_id))
    conn.commit()
    generate = crawlers.get_generator(conn)
    # 렌더로 가져오는 사이트는 렌더로만 만든다. 정적 사이트는 모드를 비워 등록처럼 스스로 정하게
    # 한다 — 정적으로 묶으면 목록을 JS 로 그리게 바뀐 사이트를 영원히 못 찾는다 (2026-09-30, 한솔).
    # API 로 받는 목록은 셀렉터가 없어 경로를 정하게 비워 둔다
    mode = str(row["list_mode"] or "")
    try:
        result = await generate(str(row["list_url"]), url, mode if mode == PLAYWRIGHT else "")
    except HTTPException as exc:
        return repair_panel(request, conn, crawler_id, error=error_detail(exc))
    except Exception as exc:  # 생성 경로의 예외는 종류가 많다. 사유를 그대로 화면에 올린다
        logger.warning("사이트 %s: 공고 주소로 셀렉터를 만들지 못했다: %s", workflow_id, exc)
        return repair_panel(
            request,
            conn,
            crawler_id,
            error={"reason": "api_error", "message": f"{type(exc).__name__}: {exc}"},
        )
    failed = list(result.verification.failed)
    notice = (
        "예시 공고 주소로 셀렉터를 다시 만들었다. 아직 저장하지 않았다 — 아래 편집기에서 저장한 뒤 "
        "다시 실행해 확인한다"
    )
    if failed:
        notice += f". 만들었지만 여전히 못 찾는 칸: {', '.join(failed)}"
    # API 목록은 수집 모드를 여기서 바꾸지 않는다. 셀렉터 없이 도는 다른 경로다
    switched = result.render_mode if mode != "api" and result.render_mode != mode else ""
    if switched:
        notice += f". 정적 HTML 에 목록이 없어 {mode_word(switched)} HTML 로 만들었다"
    return repair_panel(
        request,
        conn,
        crawler_id,
        notice=notice,
        selectors_json=json.dumps(result.selectors.model_dump(), ensure_ascii=False, indent=2),
        render_mode=switched,
    )
