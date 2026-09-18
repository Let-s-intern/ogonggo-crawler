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
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.api import crawlers, site_adds, workflows
from app.api.ui import render
from app.api.ui_crawlers import error_detail
from app.api.ui_tests import repair_panel
from app.api.ui_workflows import TRIGGER_WORDS, UNKNOWN_TRIGGER, CardView, _last_run, _view
from app.scheduler import WorkflowScheduler

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
    next_runs = scheduler.next_run_times()
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
) -> HTMLResponse:
    row = conn.execute(
        "SELECT detail_url FROM crawlers WHERE id = ?", (card.item.crawler_id,)
    ).fetchone()
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
    # 목록을 렌더로 가져오는 사이트는 상세도 렌더로 만든다. API 로 받는 목록은 셀렉터가 없어
    # 경로를 정하게 비워 둔다
    mode = str(row["list_mode"] or "")
    try:
        result = await generate(str(row["list_url"]), url, "" if mode == "api" else mode)
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
    return repair_panel(
        request,
        conn,
        crawler_id,
        notice=notice,
        selectors_json=json.dumps(result.selectors.model_dump(), ensure_ascii=False, indent=2),
    )
