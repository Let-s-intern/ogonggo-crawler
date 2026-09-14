"""AI 규칙 화면의 조각 라우트 (2026-09-14 결정).

판을 쌓고 읽는 일은 `app/classify/prompt_rules.py` 가 한다. 이 파일이 더하는 것은 화면이 필요로
하는 둘이다 — 폼을 규칙 한 벌로 읽고 되그리는 것, 그리고 저장하기 전에 공고 하나로 시험하는 것.

## 시험은 저장하지 않는다

시험 분류는 폼에 적힌 규칙(저장하지 않은 것 포함)으로 모델을 실제로 부르고, 결과를 지금 저장된
분류와 칸마다 나란히 놓는다. `job_classifications`·`normalized_jobs` 에는 아무것도 쓰지 않는다 —
시험을 누를 때마다 검수 화면의 값이 바뀌면 시험이 아니다.

모델 호출 기록(`llm_calls`)에는 남긴다. 토큰을 실제로 썼고, 빼면 호출 합이 청구와 어긋난다. 판
번호는 NULL 이다 — 저장하지 않은 규칙이라 가리킬 판이 없다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from itertools import zip_longest
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app import industries, taxonomy
from app.api.settings import get_connection
from app.api.ui import render
from app.classify import prompt_rules
from app.classify.classifier import ClassifyError, classify_body
from app.classify.schema import build_classification_model
from app.classify.store import (
    read_classification,
    read_current_values,
    read_parts,
    read_source,
    read_title,
)
from app.config import Settings
from app.llm import settings as llm_settings
from app.llm.base import Usage
from app.llm.log import CLASSIFY, record_call

router = APIRouter(tags=["ui"], include_in_schema=False)

# 시험에 고를 수 있는 공고 수. 최근에 분류된 것부터다
TRIAL_CHOICES = 30
# 예시 표 끝에 붙여 두는 빈 줄 수. 줄을 늘리는 스크립트 없이 저장할 때마다 둘까지 더한다
BLANK_EXAMPLE_ROWS = 2


def get_classify_client() -> Any | None:
    """시험 분류가 쓸 모델 클라이언트. None 이면 설정으로 만든다. 테스트가 가짜로 바꾸는 자리다."""
    return None


def get_base_settings() -> Settings | None:
    """시험 분류의 바탕 설정. None 이면 환경변수다. 테스트가 키가 든 설정으로 바꾸는 자리다."""
    return None


@dataclass(frozen=True)
class TrialChoice:
    """시험에 고를 수 있는 공고 하나. 지금 값을 만든 판도 함께 적는다."""

    raw_job_id: int
    title: str
    rules_version: int | None


@dataclass(frozen=True)
class FieldDiff:
    """칸 하나의 지금 값과 시험 값."""

    name: str
    label: str
    stored: str
    trial: str

    @property
    def changed(self) -> bool:
        return self.stored.strip() != self.trial.strip()


@dataclass(frozen=True)
class PartDiff:
    """나눈 공고 하나의 비교. 저장된 분류가 없는 번호는 `classified` 가 거짓이다."""

    part: int
    role: str
    classified: bool
    fields: list[FieldDiff]

    @property
    def changed_count(self) -> int:
        return sum(1 for field in self.fields if field.changed)


def _trial_choices(conn: sqlite3.Connection) -> list[TrialChoice]:
    rows = conn.execute(
        "SELECT raw_job_id, rules_version FROM job_classifications WHERE part = 1"
        " ORDER BY classified_at DESC, raw_job_id DESC LIMIT ?",
        (TRIAL_CHOICES,),
    ).fetchall()
    return [
        TrialChoice(
            int(row["raw_job_id"]),
            read_title(conn, int(row["raw_job_id"])),
            None if row["rules_version"] is None else int(row["rules_version"]),
        )
        for row in rows
    ]


def _form(
    request: Request,
    conn: sqlite3.Connection,
    *,
    draft: prompt_rules.RuleSet | None = None,
    message: str = "",
    error: dict[str, str] | None = None,
) -> HTMLResponse:
    """편집 조각 하나. 저장·되돌리기가 모두 이 조각으로 돌아온다.

    저장이 거절되면 `draft` 로 친 내용을 그대로 되그린다. 지금 판으로 되그리면 고친 것이 사라진다.
    """
    version: prompt_rules.RuleVersion | None
    try:
        version = prompt_rules.current(conn)
    except prompt_rules.RuleSetError as exc:
        version = None
        error = error or {"reason": exc.reason, "message": str(exc)}
    if draft is not None:
        shown = draft
    elif version is not None:
        shown = version.rules
    else:
        shown = prompt_rules.DEFAULT_RULES
    return render(
        request,
        "fragments/prompt_rules_form.html",
        rules=shown,
        fields=prompt_rules.RULE_FIELDS,
        version=version,
        history=prompt_rules.history(conn),
        choices=_trial_choices(conn),
        blank_rows=range(BLANK_EXAMPLE_ROWS),
        limits={
            "common": prompt_rules.MAX_COMMON_CHARS,
            "rule": prompt_rules.MAX_RULE_CHARS,
            "examples": prompt_rules.MAX_EXAMPLES,
            "note": prompt_rules.MAX_NOTE_CHARS,
        },
        message=message,
        error=error,
    )


@router.get("/ui/prompt-rules", response_class=HTMLResponse)
def prompt_rules_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> HTMLResponse:
    return _form(request, conn)


@router.put("/ui/prompt-rules", response_class=HTMLResponse)
async def save_prompt_rules_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> HTMLResponse:
    """새 판으로 저장한다. 이미 분류된 공고는 운영자가 다시 분류할 때만 새 규칙을 쓴다."""
    form = await request.form()
    draft = prompt_rules.parse_form(form)
    note = form.get("note")
    try:
        saved = prompt_rules.save(conn, draft, note if isinstance(note, str) else "")
    except prompt_rules.RuleSetError as exc:
        error = {"reason": exc.reason, "message": str(exc)}
        return _form(request, conn, draft=draft, error=error)
    return _form(
        request,
        conn,
        message=(
            f"판 {saved.number} 로 저장했다. 다음 분류부터 이 규칙을 쓴다 — "
            "이미 분류된 공고는 다시 분류해야 바뀐다"
        ),
    )


@router.post("/ui/prompt-rules/{number}/restore", response_class=HTMLResponse)
def restore_prompt_rules_fragment(
    request: Request,
    number: int,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> HTMLResponse:
    """옛 판을 복사해 새 판으로 저장한다. 옛 판은 이력에 그대로 남는다."""
    try:
        saved = prompt_rules.restore(conn, number)
    except prompt_rules.RuleSetError as exc:
        return _form(request, conn, error={"reason": exc.reason, "message": str(exc)})
    return _form(request, conn, message=f"판 {number} 을 판 {saved.number} 로 복사해 되돌렸다")


@router.post("/ui/prompt-rules/try", response_class=HTMLResponse)
async def try_prompt_rules_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    client: Annotated[Any | None, Depends(get_classify_client)],
    base_settings: Annotated[Settings | None, Depends(get_base_settings)],
) -> HTMLResponse:
    """폼의 규칙으로 공고 하나를 분류해 지금 저장된 값과 칸마다 비교한다. 결과는 저장하지 않는다."""
    form = await request.form()
    draft = prompt_rules.parse_form(form)
    raw = form.get("raw_job_id")
    try:
        prompt_rules.validate(draft)
    except prompt_rules.RuleSetError as exc:
        return _trial_error(request, exc.reason, str(exc))
    if not isinstance(raw, str) or not raw.strip().isdigit():
        return _trial_error(request, "no_posting", "시험할 공고를 고르지 않았다")
    raw_job_id = int(raw)
    title = read_title(conn, raw_job_id)
    stored = read_parts(conn, raw_job_id)
    # 배치와 같은 조건이다. 이미 나눈 공고는 나눈 목록을 고정한 채로 칸만 다시 채운다
    # (`app/classify/batch.py`)
    known = stored if len(stored) > 1 or any(part.lines for part in stored) else []
    usages: list[Usage] = []

    def counted(usage: Usage) -> None:
        usages.append(usage)
        record_call(conn, feature=CLASSIFY, usage=usage)

    try:
        result = await classify_body(
            read_source(conn, raw_job_id),
            title=title,
            current_values=read_current_values(conn, raw_job_id),
            taxonomy_tree=taxonomy.enabled_tree(conn),
            industries=industries.enabled_names(conn),
            response_model=build_classification_model(conn),
            known_parts=[(part.role, part.lines) for part in known],
            settings=llm_settings.settings_for(conn, CLASSIFY, base_settings),
            client=client,
            on_call=counted,
            rules=draft,
        )
    except ClassifyError as exc:
        return _trial_error(request, exc.reason, str(exc))

    parts: list[PartDiff] = []
    pairs = zip_longest(stored, result.postings)
    for number, (stored_part, posting) in enumerate(pairs, start=1):
        current = read_classification(conn, raw_job_id, number)
        trial = posting.fields if posting is not None else {}
        role = stored_part.role if stored_part is not None else ""
        if not role and posting is not None and len(result.postings) > 1:
            role = posting.fields.get("position_name", "").strip()
        parts.append(
            PartDiff(
                number,
                role,
                bool(current),
                [
                    FieldDiff(name, label, current.get(name, ""), trial.get(name, ""))
                    for name, label, _ in prompt_rules.RULE_FIELDS
                ],
            )
        )
    return render(
        request,
        "fragments/prompt_rules_try.html",
        raw_job_id=raw_job_id,
        title=title,
        parts=parts,
        notes=result.notes,
        calls=len(usages),
        tokens=sum(usage.total_tokens for usage in usages),
        error=None,
    )


def _trial_error(request: Request, reason: str, message: str) -> HTMLResponse:
    return render(
        request, "fragments/prompt_rules_try.html", error={"reason": reason, "message": message}
    )
