"""워크플로우 승격과 운영.

승격은 크롤러를 주기 실행 대상으로 올리는 단계다. `crawlers.status=tested` 인 것만 올라간다 —
테스트를 거치지 않은 셀렉터는 가설일 뿐이고, 가설을 주기 실행에 걸면 실패가 사이트 부하로
쌓인다 (`.claude/rules/llm.md`, `.claude/docs/data-model.md`).

이미 `promoted` 인 크롤러는 다시 승격하지 않는다. 같은 크롤러에 워크플로우가 둘이면 같은
목록 페이지를 두 배로 때린다.

테이블을 바꾼 요청은 반드시 스케줄러 `sync()` 까지 간다. 여기서 멈추면 `paused` 로 바꾼
워크플로우가 계속 깨어나고, 주기를 늘려 놓은 워크플로우가 옛 주기로 계속 사이트를 때린다.
`.claude/rules/crawling.md` 가 말하는 "테이블이 진실" 은 테이블을 바꾼 쪽이 잡까지 맞출 때만
사실이다.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from app import db
from app.scheduler import WorkflowScheduler, get_scheduler

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


class WorkflowCreate(BaseModel):
    """승격 요청. 이름과 주기를 받는다."""

    crawler_id: int
    name: str = ""
    interval_minutes: int = Field(default=360, ge=1)
    # NULL 이면 자동 중지하지 않는다
    auto_stop_threshold: int | None = Field(default=None, ge=1)


class WorkflowOut(BaseModel):
    id: int
    crawler_id: int
    name: str
    interval_minutes: int
    status: str
    auto_stop_threshold: int | None
    crawler_status: str


class WorkflowUpdate(BaseModel):
    """주기 변경, 수동 중지·재개, 자동 중지 임계치. 전부 비면 바꿀 것이 없다.

    임계치만 `None` 이 뜻을 갖는다 — "자동으로 멈추지 않는다" 이고, 그것을 보내는 것 자체가
    변경이다. 그래서 값이 `None` 인지가 아니라 **요청에 들어 있었는지**(`model_fields_set`)로
    가른다. 값으로만 가르면 임계치를 지우는 요청과 임계치를 건드리지 않는 요청이 같아진다.
    """

    interval_minutes: int | None = Field(default=None, ge=1)
    status: Literal["active", "paused"] | None = None
    auto_stop_threshold: int | None = Field(default=None, ge=1)

    def changes_threshold(self) -> bool:
        return "auto_stop_threshold" in self.model_fields_set

    @model_validator(mode="after")
    def at_least_one(self) -> WorkflowUpdate:
        if self.interval_minutes is None and self.status is None and not self.changes_threshold():
            raise ValueError("interval_minutes, status, auto_stop_threshold 중 하나는 있어야 한다")
        return self


class WorkflowItem(BaseModel):
    """목록 한 줄. 운영자가 화면에서 보는 값 그대로다."""

    id: int
    crawler_id: int
    name: str
    list_url: str
    interval_minutes: int
    status: str
    auto_stop_threshold: int | None
    last_run_at: str | None
    last_run_status: str | None
    success_count: int
    fail_count: int


def get_connection() -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def get_workflow_scheduler() -> WorkflowScheduler:
    """앱이 쓰는 스케줄러. 테스트는 이 의존성을 갈아끼운다."""
    return get_scheduler()


_LIST_QUERY = """
    SELECT w.id                AS id,
           w.crawler_id        AS crawler_id,
           w.name              AS name,
           c.list_url          AS list_url,
           w.interval_minutes  AS interval_minutes,
           w.status            AS status,
           w.auto_stop_threshold AS auto_stop_threshold,
           w.last_run_at       AS last_run_at,
           w.success_count     AS success_count,
           w.fail_count        AS fail_count,
           (SELECT r.status
              FROM crawl_runs r
             WHERE r.workflow_id = w.id AND r.status IS NOT NULL
             ORDER BY r.id DESC LIMIT 1) AS last_run_status
      FROM workflows w
      JOIN crawlers c ON c.id = w.crawler_id
"""


def _item(row: sqlite3.Row) -> WorkflowItem:
    return WorkflowItem(**dict(row))


@router.post("", response_model=WorkflowOut, status_code=201)
def promote(
    payload: WorkflowCreate,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    scheduler: Annotated[WorkflowScheduler, Depends(get_workflow_scheduler)],
) -> WorkflowOut:
    """`tested` 크롤러를 워크플로우로 올리고 크롤러를 `promoted` 로 바꾼다."""
    row = conn.execute(
        "SELECT name, status FROM crawlers WHERE id = ?", (payload.crawler_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404, detail={"message": f"크롤러 {payload.crawler_id} 가 없다"}
        )
    if row["status"] != "tested":
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "not_tested",
                "message": (
                    f"승격은 status=tested 인 크롤러만 가능하다. 현재 상태는 {row['status']} 다"
                ),
            },
        )

    name = payload.name.strip() or str(row["name"])
    cursor = conn.execute(
        """
        INSERT INTO workflows (crawler_id, name, interval_minutes, status, auto_stop_threshold)
        VALUES (?, ?, ?, 'active', ?)
        """,
        (payload.crawler_id, name, payload.interval_minutes, payload.auto_stop_threshold),
    )
    conn.execute("UPDATE crawlers SET status = 'promoted' WHERE id = ?", (payload.crawler_id,))
    # 승격은 곧 active 다. 다음 기동까지 기다리지 않고 지금 잡을 만든다
    scheduler.sync(conn)

    return WorkflowOut(
        id=int(cursor.lastrowid or 0),
        crawler_id=payload.crawler_id,
        name=name,
        interval_minutes=payload.interval_minutes,
        status="active",
        auto_stop_threshold=payload.auto_stop_threshold,
        crawler_status="promoted",
    )


@router.get("", response_model=list[WorkflowItem])
def list_workflows(
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> list[WorkflowItem]:
    """등록된 사이트 워크플로우 전부. 이름, 대상, 주기, 최근 실행, 누적 성공·실패.

    주소로 직접 넣은 공고를 담는 워크플로우(`kind = 'manual'`)는 빼 둔다. 돌 목록이 없는 자리라
    사이트 목록에 섞이면 켜고 끌 수 있는 사이트로 보인다 (`migrations/0042_manual_workflow.sql`).
    """
    rows = conn.execute(_LIST_QUERY + " WHERE w.kind = 'crawl' ORDER BY w.id").fetchall()
    return [_item(row) for row in rows]


@router.patch("/{workflow_id}", response_model=WorkflowItem)
def update_workflow(
    workflow_id: int,
    payload: WorkflowUpdate,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    scheduler: Annotated[WorkflowScheduler, Depends(get_workflow_scheduler)],
) -> WorkflowItem:
    """주기 변경, 수동 중지·재개, 자동 중지 임계치. 바뀐 내용은 그대로 스케줄러 잡까지 간다."""
    row = conn.execute("SELECT id FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404, detail={"message": f"워크플로우 {workflow_id} 가 없다"}
        )

    if payload.interval_minutes is not None:
        conn.execute(
            "UPDATE workflows SET interval_minutes = ? WHERE id = ?",
            (payload.interval_minutes, workflow_id),
        )
    if payload.status is not None:
        conn.execute("UPDATE workflows SET status = ? WHERE id = ?", (payload.status, workflow_id))
    if payload.changes_threshold():
        # NULL 이면 자동 중지하지 않는다. 판정과 중지는 실행 쪽이 한다
        # (`app/crawler/runner.py` 의 `_record_outcome`)
        conn.execute(
            "UPDATE workflows SET auto_stop_threshold = ? WHERE id = ?",
            (payload.auto_stop_threshold, workflow_id),
        )

    scheduler.sync(conn)

    updated = conn.execute(_LIST_QUERY + " WHERE w.id = ?", (workflow_id,)).fetchone()
    return _item(updated)
