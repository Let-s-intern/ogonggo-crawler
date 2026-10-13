"""렛커 콘텐츠 화면의 조각 라우트 (2026-10-13 결정, LC-3457).

오공고 공고 상세에 추천하는 렛츠커리어 콘텐츠(프로그램·무료 자료집·블로그)에 어떤 태그가
붙었는지 본다. 태그는 오공고에 저장돼 있어 화면을 열 때마다 오공고에서 받아 그린다. 크롤러는
따로 남기지 않는다. 태그는 매일 공고 분석 잡이 먼저 붙이고(`app/letscareer_tags/runner.py`),
여기서 지금 붙일 수도 있다.
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections import Counter
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from app import db
from app.api.settings import get_connection
from app.api.ui import render
from app.config import Settings, get_settings
from app.job_analysis import ogonggo as job_analysis_ogonggo
from app.letscareer_tags import ogonggo, runner
from app.letscareer_tags.tagger import TOPICS

router = APIRouter(tags=["ui"], include_in_schema=False)

LETSCAREER_WEB = "https://www.letscareer.co.kr"

KINDS: dict[str, str] = {
    "CHALLENGE": "챌린지",
    "LIVE": "라이브",
    "VOD": "VOD",
    "GUIDEBOOK": "가이드북",
    "REPORT": "서류 진단",
    "MATERIAL": "자료집",
    "BLOG": "블로그",
}
FIELDS: dict[str, str] = {
    "IT_DEVELOPMENT": "IT·개발",
    "MARKETING_ADVERTISING": "마케팅·광고",
    "SALES": "영업",
    "HR_GENERAL_AFFAIRS": "HR·총무",
    "PLANNING_STRATEGY": "기획·전략",
}
# 준비 단계는 괄호 앞 짧은 이름만 쓴다
TOPIC_LABELS: dict[str, str] = {name: label.split("(")[0] for name, label in TOPICS.items()}
# 직군 필터의 '공통' 은 직군 태그가 없어 어느 공고에나 나오는 콘텐츠다
COMMON = "COMMON"

_task: asyncio.Task[None] | None = None


async def _panel(
    request: Request,
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    kind: str = "",
    field: str = "",
    status: str = "",
    message: str = "",
) -> HTMLResponse:
    error = ""
    items: list[dict[str, Any]] = []
    try:
        async with job_analysis_ogonggo.connection(conn, settings) as client:
            items = await ogonggo.contents(client)
    except ogonggo.OgonggoUnavailable as exc:
        error = str(exc)

    counts = Counter("tagged" if item.get("tagged") else "untagged" for item in items)
    shown = [item for item in items if _matches(item, kind, field, status)]
    return render(
        request,
        "fragments/letscareer_contents_panel.html",
        items=shown,
        total=len(items),
        counts=counts,
        filters={"kind": kind, "field": field, "status": status},
        kinds=KINDS,
        fields={**FIELDS, COMMON: "공통(직군 없음)"},
        topics=TOPIC_LABELS,
        web=LETSCAREER_WEB,
        running=_running(),
        message=message,
        error=error,
    )


def _matches(item: dict[str, Any], kind: str, field: str, status: str) -> bool:
    if kind and item.get("kind") != kind:
        return False
    if status == "tagged" and not item.get("tagged"):
        return False
    if status == "untagged" and item.get("tagged"):
        return False
    if field:
        fields = item.get("jobFields") or []
        if field == COMMON:
            return bool(item.get("tagged")) and not fields
        return field in fields
    return True


def _running() -> bool:
    return _task is not None and not _task.done()


@router.get("/ui/letscareer-contents", response_class=HTMLResponse)
async def letscareer_contents_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
    kind: Annotated[str, Query()] = "",
    field: Annotated[str, Query()] = "",
    status: Annotated[str, Query()] = "",
) -> HTMLResponse:
    return await _panel(request, conn, settings, kind=kind, field=field, status=status)


@router.post("/ui/letscareer-contents/tag", response_class=HTMLResponse)
async def tag_letscareer_contents_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """태그가 없거나 내용이 바뀐 콘텐츠에 지금 태그를 붙인다.

    백그라운드에서 돌고 화면은 몇 초마다 다시 그린다.
    """
    global _task
    if _running():
        return await _panel(request, conn, settings, message="이미 태그를 붙이는 중이에요")

    async def run() -> None:
        background = db.connect()
        try:
            await runner.run_once(background, settings=settings)
        finally:
            background.close()

    _task = asyncio.create_task(run())
    return await _panel(
        request, conn, settings, message="태그를 붙이기 시작했어요. 끝나면 목록에 바로 보여요"
    )
