"""공고 한 건 추가. 공고 주소 하나를 넣으면 한 번만 가져와 저장하고 바로 분류한다 (2026-09-21 결정).

사이트 추가 옆에 둔다 — 등록하는 일이 한곳에 모인다. 결과는 공고 화면에서 검수하고 보낸다.
가져오기와 저장은 `app/crawler/manual.py` 가 하고, 이 파일은 분류를 붙이고 결과를 그린다.

**가져와 저장하는 것까지만 창에서 기다리고, 분류는 요청 밖에서 돈다.** 처음에는 분류까지 창에서
기다리게 했는데, 동원 하반기 신입 공채 페이지(6개사 × 11개 직무, 본문 2만 9천 자)는 분류가 직무마다
나뉘어 AI 호출 22번에 16분이 걸렸다(2026-09-21 실측). 그 사이 화면 요청이 끊긴다. 결과는 공고
화면에서 본다 — 분류가 끝나면 칸이 채워진다.

분류는 다른 분류가 돌고 있으면 붙이지 않는다. 같은 공고를 두 실행이 동시에 쓰게 되기 때문이다
(`app/api/review_actions.py` 의 `job_reclassify` 와 같은 검사). 그때 공고는 저장만 되고, 수집 직후
분류가 아직 분류 안 된 공고로 집어 간다.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.api import crawlers
from app.api.ui import render
from app.api.ui_site_add import get_add_connect, get_add_launcher
from app.classify.batch import ClassifyProgress, classify_ids, get_classify_run
from app.classify.schema import EMPLOYMENT_TYPES, EXPERIENCE_TYPES
from app.crawler.fetcher import PageSource, get_fetcher
from app.crawler.images import LlmImageReader
from app.crawler.manual import ManualAddError, add_posting
from app.crawler.playwright import Renderer
from app.side import runner as side_runner

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ui"], include_in_schema=False)

# 결과에 보이는 칸. 분류가 채운 것이 맞게 들어갔는지 한눈에 본다
_SHOWN = (
    "SELECT id, part, title, company_name, job_field, job_role, recruitment_end_at,"
    " employment_type, experience_type FROM normalized_jobs WHERE raw_job_id = ? ORDER BY part"
)


def _form(request: Request, **context: object) -> HTMLResponse:
    return render(request, "fragments/posting_add.html", **context)


@router.get("/ui/postings/new", response_class=HTMLResponse)
def posting_add_form(request: Request) -> HTMLResponse:
    return _form(request, step="form", url="", company="")


@router.post("/ui/postings/new", response_class=HTMLResponse)
async def posting_add(
    request: Request,
    url: Annotated[str, Form()],
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
    launch: Annotated[Callable[[Coroutine[Any, Any, None]], None], Depends(get_add_launcher)],
    connect: Annotated[Callable[[], sqlite3.Connection], Depends(get_add_connect)],
    company: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """가져와 저장하고, 분류를 요청 밖에 건다. 어느 단계에서 멈췄는지를 결과에 적는다."""
    fetcher = get_fetcher()
    renderer: Renderer | None = None

    async def open_renderer() -> PageSource:
        # 정적 HTML 이 껍데기일 때만 여기까지 온다. 브라우저는 이때 뜬다
        nonlocal renderer
        renderer = Renderer(fetcher)
        return renderer

    try:
        added = await add_posting(
            conn,
            url,
            company=company,
            fetcher=fetcher,
            open_renderer=open_renderer,
            reader=LlmImageReader(conn),
        )
    except ManualAddError as exc:
        return _form(request, step="form", url=url, company=company, error=str(exc))
    finally:
        if renderer is not None:
            await renderer.aclose()

    notes = list(added.notes)
    classifying = False
    if not added.existed:
        busy = _busy(conn)
        if busy:
            notes.append(f"지금은 분류를 걸지 못해 저장만 했다({busy}). 다음 분류 때 채워진다")
        else:
            launch(_classify(connect, added.raw_job_id))
            classifying = True
    return _form(
        request,
        step="done",
        added=added,
        rows=conn.execute(_SHOWN, (added.raw_job_id,)).fetchall(),
        notes=notes,
        classifying=classifying,
        # 저장값은 오공고 enum 이름이다. 화면에는 한글 이름으로 보인다
        labels={**EMPLOYMENT_TYPES, **EXPERIENCE_TYPES},
    )


def _busy(conn: sqlite3.Connection) -> str | None:
    """다른 분류가 돌고 있으면 그 사유. 같은 공고를 두 실행이 동시에 쓰지 않게 한다."""
    return side_runner.classify_running(conn) or (
        "분류 실행이 아직 돌고 있다" if get_classify_run().progress().running else None
    )


async def _classify(connect: Callable[[], sqlite3.Connection], raw_job_id: int) -> None:
    """그 공고 하나를 요청 밖에서 분류한다. 실패는 로그와 공고 화면의 빈 칸으로 남는다.

    연결은 새로 연다. 요청의 연결은 응답과 함께 닫힌다.
    """
    conn = connect()
    try:
        progress = await classify_ids(conn, [raw_job_id], ClassifyProgress())
        if not progress.processed:
            logger.warning(
                "공고 한 건 추가: 분류 실패 raw_job_id=%s: %s",
                raw_job_id,
                "; ".join(progress.errors) or "사유 없음",
            )
    except Exception:  # noqa: BLE001 — 요청 밖이라 여기서 잡지 않으면 아무도 모른다
        logger.exception("공고 한 건 추가: 분류 중 예상하지 못한 오류 raw_job_id=%s", raw_job_id)
    finally:
        conn.close()
