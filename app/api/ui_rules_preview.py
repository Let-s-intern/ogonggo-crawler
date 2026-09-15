"""규칙 미리보기. 저장하기 전에 무슨 일이 일어나는지 보여 준다.

정규식 하나를 잘못 쓰면 값이 조용히 지워진다. 여기서는 아무것도 저장하지 않는다. DB 는 읽기만
한다. 고를 수 있는 칸은 규칙 목록과 같은 수집 칸 다섯이다 (`app/normalize/rules.py` 의
`RULE_FIELDS`).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from app.api import rules
from app.api.ui import render
from app.api.ui_rules import _config
from app.normalize.engine import _apply, _by_field, load_rules
from app.normalize.rules import (
    RULE_FIELD_LABELS,
    RULE_FIELDS,
    RULE_TYPES,
    Rule,
    RuleConfigError,
    build_rule,
)

router = APIRouter(tags=["ui"], include_in_schema=False)


@dataclass(frozen=True)
class Step:
    """규칙 하나를 지난 결과. `changed` 가 거짓이면 그 규칙은 이 값에 아무 일도 하지 않았다."""

    label: str
    rule_type: str
    value: str
    changed: bool
    error: str = ""


def _steps(sample: str, applied: list[Rule]) -> tuple[list[Step], str | None]:
    """규칙을 순서대로 적용하며 매 단계를 기록한다.

    엔진과 같은 규칙으로 멈춘다 — 값이 비면 뒤 규칙에 넘기지 않는다.
    """
    steps: list[Step] = []
    value = sample
    for rule in applied:
        label = f"{rule.rule_type} (우선순위 {rule.priority})"
        try:
            after = _apply(value, rule)
        except Exception as exc:  # 규칙 하나가 실패하면 거기서 끝난다
            steps.append(Step(label, rule.rule_type, value, False, str(exc)))
            return steps, None
        steps.append(Step(label, rule.rule_type, after, after != value))
        value = after
        if not value:
            steps.append(Step("값이 비어 여기서 멈춘다", "", "", False))
            return steps, None
    return steps, value or None


@router.get("/ui/rules/preview", response_class=HTMLResponse)
def preview_initial_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(rules.get_connection)],
) -> HTMLResponse:
    """빈 미리보기. 화면이 처음 열릴 때 폼만 그린다."""
    return preview_fragment(request, conn, field_name=RULE_FIELDS[0])


@router.post("/ui/rules/preview", response_class=HTMLResponse)
def preview_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(rules.get_connection)],
    field_name: Annotated[str, Form()],
    sample: Annotated[str, Form()] = "",
    draft_type: Annotated[str, Form()] = "",
    draft_config: Annotated[str, Form()] = "",
    draft_priority: Annotated[int, Form()] = 0,
) -> HTMLResponse:
    """저장된 규칙에 아직 저장하지 않은 규칙 하나를 얹어 결과를 보여 준다."""
    error = ""
    draft: Rule | None = None
    if draft_type:
        try:
            draft = build_rule(
                field_name=field_name,
                rule_type=draft_type,
                config=_config(draft_config),
                priority=draft_priority,
                rule_id=None,
            )
        except (RuleConfigError, ValueError) as exc:
            error = str(exc)

    # 엔진이 쓰는 바로 그 경로로 읽는다. 미리보기가 실제 동작과 어긋나면 믿을 수 없다.
    stored = [rule for rule in load_rules(conn) if rule.field_name == field_name]
    applied = _by_field([*stored, *([draft] if draft else [])]).get(field_name, [])

    steps: list[Step] = []
    result: str | None = None
    if sample and not error:
        steps, result = _steps(sample, applied)

    return render(
        request,
        "fragments/rule_preview.html",
        field_name=field_name,
        field_label=RULE_FIELD_LABELS.get(field_name, field_name),
        fields=RULE_FIELDS,
        labels=RULE_FIELD_LABELS,
        rule_types=RULE_TYPES,
        sample=sample,
        draft_type=draft_type,
        draft_config=draft_config,
        draft_priority=draft_priority,
        rule_count=len(applied),
        steps=steps,
        result=result,
        error=error,
    )
