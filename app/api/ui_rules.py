"""정규화 규칙 화면과 재정규화 화면의 조각 라우트.

규칙 CRUD 는 `app/api/rules.py` 를, 재정규화는 같은 파일의 renormalize 라우트를 그대로 부른다.

## 고를 수 있는 칸은 수집 칸 다섯이다

AI 분류가 덮어쓰는 칸에 건 규칙은 아무 일도 하지 않는다 (`app/normalize/rules.py` 의
`RULE_FIELDS`). 그런 칸에 이미 걸려 있던 규칙은 0037 이 껐고, 목록에서는 `효과 없음` 으로 보이며
지우기만 할 수 있다.

## 규칙 편집과 재정규화는 분리되어 있다

규칙을 저장해도 기존 `normalized_jobs` 는 그대로다. 저장 경로 어디에서도 재정규화를 부르지
않는다 (2026-08-21 결정, `app/normalize/backfill.py`). 재정규화는 누르자마자 도는 것이 아니라
대상 건수를 먼저 보여준다.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from app.api import rules
from app.api.ui import render
from app.api.ui_crawlers import error_detail
from app.normalize.backfill import Backfill, ConnectFactory
from app.normalize.rules import RULE_FIELD_LABELS, RULE_FIELDS, RULE_TYPES

router = APIRouter(tags=["ui"], include_in_schema=False)

# 타입별로 무엇을 적어야 하는지. `app/normalize/rules.py` 의 표와 같은 내용이다
CONFIG_HINTS: tuple[tuple[str, str], ...] = (
    ("mapping", '{"map": {"원문": "바꿀 값"}, "default": null}'),
    ("regex", '{"pattern": "\\\\s+", "replacement": " "}'),
    ("trim", '{"collapse_whitespace": true, "strip_chars": null}'),
    ("date_parse", '{"formats": ["%Y-%m-%d"], "output_format": "%Y-%m-%d %H:%M:%S"}'),
)


def _rule_list(
    request: Request,
    conn: sqlite3.Connection,
    *,
    message: str = "",
    error: dict[str, str] | None = None,
) -> HTMLResponse:
    """규칙 목록 하나. 추가·수정·삭제·토글이 모두 이 조각으로 돌아온다."""
    return render(
        request,
        "fragments/rule_list.html",
        rules=rules.list_rules(conn),
        fields=RULE_FIELDS,
        labels=RULE_FIELD_LABELS,
        rule_types=RULE_TYPES,
        hints=CONFIG_HINTS,
        message=message,
        error=error,
    )


def _config(raw: str) -> dict[str, Any]:
    """설정 JSON 을 읽는다. 못 읽으면 그대로 알린다 — 추측해서 고치지 않는다."""
    parsed = json.loads(raw or "{}")
    if not isinstance(parsed, dict):
        raise ValueError(f"설정은 객체여야 한다: {type(parsed).__name__}")
    return parsed


@router.get("/ui/rules", response_class=HTMLResponse)
def rule_list_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(rules.get_connection)],
) -> HTMLResponse:
    return _rule_list(request, conn)


@router.post("/ui/rules", response_class=HTMLResponse)
def create_rule_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(rules.get_connection)],
    field_name: Annotated[str, Form()],
    rule_type: Annotated[str, Form()],
    rule_config: Annotated[str, Form()] = "{}",
    priority: Annotated[int, Form()] = 0,
    enabled: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """규칙을 추가한다. 이 규칙은 다음에 정규화되는 건부터 적용된다 — 기존 데이터는 그대로다."""
    try:
        payload = rules.RuleCreate(
            field_name=field_name,
            rule_type=rule_type,
            rule_config=_config(rule_config),
            note=note,
            priority=priority,
            enabled=bool(enabled),
        )
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        return _rule_list(request, conn, error={"reason": "invalid_config", "message": str(exc)})

    try:
        created = rules.create_rule(payload, conn)
    except HTTPException as exc:
        return _rule_list(request, conn, error=error_detail(exc))

    return _rule_list(request, conn, message=f"규칙 {created.id} 를 추가했다")


@router.put("/ui/rules/{rule_id}", response_class=HTMLResponse)
def update_rule_fragment(
    request: Request,
    rule_id: int,
    conn: Annotated[sqlite3.Connection, Depends(rules.get_connection)],
    field_name: Annotated[str, Form()],
    rule_type: Annotated[str, Form()],
    rule_config: Annotated[str, Form()] = "{}",
    priority: Annotated[int, Form()] = 0,
    enabled: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
) -> HTMLResponse:
    """규칙을 고친다. 우선순위도 여기서 바뀐다."""
    try:
        payload = rules.RuleUpdate(
            field_name=field_name,
            rule_type=rule_type,
            rule_config=_config(rule_config),
            note=note,
            priority=priority,
            enabled=bool(enabled),
        )
    except (json.JSONDecodeError, ValidationError, ValueError) as exc:
        return _rule_list(request, conn, error={"reason": "invalid_config", "message": str(exc)})

    try:
        updated = rules.update_rule(rule_id, payload, conn)
    except HTTPException as exc:
        return _rule_list(request, conn, error=error_detail(exc))

    return _rule_list(request, conn, message=f"규칙 {updated.id} 를 저장했다")


@router.post("/ui/rules/{rule_id}/toggle", response_class=HTMLResponse)
def toggle_rule_fragment(
    request: Request,
    rule_id: int,
    conn: Annotated[sqlite3.Connection, Depends(rules.get_connection)],
) -> HTMLResponse:
    """활성·비활성만 뒤집는다. 다른 값은 건드리지 않는다."""
    current = next((rule for rule in rules.list_rules(conn) if rule.id == rule_id), None)
    if current is None:
        return _rule_list(
            request, conn, error={"reason": "not_found", "message": f"규칙 {rule_id} 가 없다"}
        )

    try:
        updated = rules.update_rule(rule_id, rules.RuleUpdate(enabled=not current.enabled), conn)
    except HTTPException as exc:
        return _rule_list(request, conn, error=error_detail(exc))

    state = "켰다" if updated.enabled else "껐다"
    return _rule_list(request, conn, message=f"규칙 {rule_id} 를 {state}")


@router.delete("/ui/rules/{rule_id}", response_class=HTMLResponse)
def delete_rule_fragment(
    request: Request,
    rule_id: int,
    conn: Annotated[sqlite3.Connection, Depends(rules.get_connection)],
) -> HTMLResponse:
    """규칙을 지운다. 그 규칙으로 이미 정규화된 값은 되돌아가지 않는다."""
    try:
        rules.delete_rule(rule_id, conn)
    except HTTPException as exc:
        return _rule_list(request, conn, error=error_detail(exc))
    return _rule_list(request, conn, message=f"규칙 {rule_id} 를 지웠다")


def _panel(
    request: Request,
    backfill: Backfill,
    conn: sqlite3.Connection,
    *,
    confirming: bool = False,
    error: dict[str, str] | None = None,
) -> HTMLResponse:
    """재정규화 영역 하나. 규칙 편집과 같은 요청에서 갱신되지 않는다."""
    row = conn.execute("SELECT count(*) AS total FROM raw_jobs").fetchone()
    return render(
        request,
        "fragments/renormalize.html",
        progress=rules.read_renormalize(backfill),
        target_count=int(row["total"]) if row is not None else 0,
        confirming=confirming,
        error=error,
    )


@router.get("/ui/renormalize", response_class=HTMLResponse)
def renormalize_panel_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(rules.get_connection)],
    backfill: Annotated[Backfill, Depends(rules.get_backfill)],
    confirm: bool = False,
) -> HTMLResponse:
    """현재 상태. 실행 중이면 이 조각이 자기 자신을 2초마다 다시 부른다."""
    return _panel(request, backfill, conn, confirming=confirm)


@router.post("/ui/renormalize", response_class=HTMLResponse)
def start_renormalize_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(rules.get_connection)],
    backfill: Annotated[Backfill, Depends(rules.get_backfill)],
    connect: Annotated[ConnectFactory, Depends(rules.get_connect_factory)],
) -> HTMLResponse:
    """운영자가 확인하고 누른 경우에만 시작한다."""
    try:
        rules.start_renormalize(backfill, connect)
    except HTTPException as exc:
        return _panel(request, backfill, conn, error=error_detail(exc))
    return _panel(request, backfill, conn)
