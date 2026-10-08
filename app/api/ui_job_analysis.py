"""AI 공고 분석 화면의 조각 라우트 (2026-10-07 결정, LC-3446).

판을 쌓고 읽는 일은 `app/job_analysis/guide.py` 가 한다. 이 파일이 더하는 것은 화면이 필요로 하는
셋이다 — 폼을 분석 방법 한 벌로 읽고 되그리는 것, 올린 참고 파일을 읽는 것, 그리고 저장하기 전에
공고 하나로 시험하는 것.

## 폼 하나가 저장과 시험을 함께 보낸다

참고 파일은 판 안에 내용째 들어간다. 그래서 이미 올린 파일은 숨은 칸으로 폼에 실려 다니고, 새로 고른
파일은 저장이나 시험을 누를 때 함께 올라간다. 시험이 저장하지 않은 지침과 파일을 그대로 써야
"저장하면 이렇게 나온다" 를 미리 볼 수 있다.

## 시험은 저장하지 않는다

시험은 모델을 실제로 부르고 결과를 오공고 화면과 같은 짜임으로 그린다. 공고에는 아무것도 쓰지
않는다. 모델 호출 기록(`llm_calls`)에는 남긴다 — 토큰을 실제로 썼다. 저장하지 않은 방법으로 돌린
호출의 판 번호는 NULL 이다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from starlette.datastructures import FormData, UploadFile

from app.api.settings import get_connection
from app.api.ui import render
from app.config import Settings
from app.deliver.spring import payload
from app.job_analysis import guide as guides
from app.job_analysis.analyzer import (
    JOB_ANALYSIS,
    AnalysisError,
    AnalysisResult,
    analyze,
    build_prompt,
)
from app.job_analysis.guide import Guide, GuideError, GuideFile
from app.job_analysis.posting import Posting
from app.job_analysis.schema import EMPLOYMENT_KEYS, SUBMISSION_KEYS
from app.llm.pricing import cost_usd

router = APIRouter(tags=["ui"], include_in_schema=False)

# 시험에 고를 수 있는 공고 수. 최근에 정규화된 것부터다
TRIAL_CHOICES = 40
# 토큰 어림. 실제 호출이 아직 없을 때만 쓴다 — 한글은 대체로 1.5자에 1토큰 남짓이다
CHARS_PER_TOKEN = 1.5
# 평균을 낼 최근 호출 수
RECENT_CALLS = 30


def get_analysis_client() -> Any | None:
    """시험 분석이 쓸 모델 클라이언트. None 이면 설정으로 만든다. 테스트가 가짜로 바꾸는 자리다."""
    return None


def get_base_settings() -> Settings | None:
    """시험 분석의 바탕 설정. None 이면 환경변수다. 테스트가 키가 든 설정으로 바꾸는 자리다."""
    return None


@dataclass(frozen=True)
class TrialChoice:
    """시험에 고를 수 있는 공고 하나."""

    job_id: int
    title: str
    company: str


@dataclass(frozen=True)
class Trial:
    """시험 한 번의 결과. 비교할 때는 지금 판의 결과가 `baseline` 이다."""

    label: str
    result: AnalysisResult | None
    error: dict[str, str] | None = None

    @property
    def cost(self) -> float | None:
        if self.result is None:
            return None
        return cost_usd(self.result.model, self.result.input_tokens, self.result.output_tokens)

    @property
    def latency_ms(self) -> int:
        return sum(usage.latency_ms for usage in self.result.usages) if self.result else 0


def _choices(conn: sqlite3.Connection) -> list[TrialChoice]:
    rows = conn.execute(
        "SELECT id, title, company_name FROM normalized_jobs"
        " WHERE coalesce(responsibilities, '') != '' OR coalesce(qualifications, '') != ''"
        " ORDER BY id DESC LIMIT ?",
        (TRIAL_CHOICES,),
    ).fetchall()
    return [
        TrialChoice(int(row["id"]), str(row["title"] or ""), str(row["company_name"] or ""))
        for row in rows
    ]


def _usage(conn: sqlite3.Connection) -> dict[str, Any]:
    """최근 분석 호출의 평균 토큰과 비용. 화면 위의 요약 칸이 쓴다."""
    rows = conn.execute(
        "SELECT model, input_tokens, output_tokens FROM llm_calls"
        " WHERE feature = ? AND ok = 1 ORDER BY id DESC LIMIT ?",
        (JOB_ANALYSIS, RECENT_CALLS),
    ).fetchall()
    if not rows:
        return {"calls": 0}
    costs = [
        cost_usd(str(row["model"]), int(row["input_tokens"]), int(row["output_tokens"]))
        for row in rows
    ]
    known = [cost for cost in costs if cost is not None]
    return {
        "calls": len(rows),
        "input": round(sum(int(row["input_tokens"]) for row in rows) / len(rows)),
        "output": round(sum(int(row["output_tokens"]) for row in rows) / len(rows)),
        "cost": sum(known) / len(known) if known else None,
    }


def _guide_tokens(guide: Guide) -> int:
    """공고를 뺀 분석 방법만의 토큰 어림. 지침과 파일을 늘리면 공고마다 이만큼 더 든다."""
    chars = len(guide.system) + len(build_prompt(guide, Posting(title="", body="x")))
    return round(chars / CHARS_PER_TOKEN)


def parse_form(form: FormData) -> tuple[Guide, list[GuideFile]]:
    """폼에서 분석 방법 한 벌을 읽는다. 검사는 하지 않는다 — 틀려도 친 내용을 그대로 되그린다.

    둘째 값은 새로 올린 파일이다. 올린 파일을 읽지 못하면 `GuideError` 를 올린다.
    """
    removed = {_text(value) for value in form.getlist("remove_file")}
    names = [_text(value) for value in form.getlist("file_name")]
    contents = [_raw(value) for value in form.getlist("file_content")]
    kept = [
        GuideFile(name, content)
        for name, content in zip(names, contents, strict=False)
        if name and name not in removed
    ]
    sections = {key: _text(form.get(f"section__{key}")) for key, _, _ in guides.SECTIONS}
    return Guide(_text(form.get("system")), sections, tuple(kept)), kept


async def _uploads(form: FormData) -> list[GuideFile]:
    uploaded: list[GuideFile] = []
    for value in form.getlist("upload"):
        if not isinstance(value, UploadFile) or not value.filename:
            continue
        data = await value.read()
        if not data:
            continue
        uploaded.append(guides.decode_file(value.filename, data))
    return uploaded


async def _read(form: FormData) -> Guide:
    """폼의 지침과 남긴 파일에 새로 올린 파일을 더한다. 같은 이름이면 새 파일로 바꾼다."""
    draft, kept = parse_form(form)
    uploaded = await _uploads(form)
    names = {item.name for item in uploaded}
    files = tuple(item for item in kept if item.name not in names) + tuple(uploaded)
    return guides.with_files(draft, files)


def _form(
    request: Request,
    conn: sqlite3.Connection,
    *,
    draft: Guide | None = None,
    message: str = "",
    error: dict[str, str] | None = None,
) -> HTMLResponse:
    """편집 조각 하나. 저장·되돌리기가 모두 이 조각으로 돌아온다.

    저장이 거절되면 `draft` 로 친 내용을 그대로 되그린다. 지금 판으로 되그리면 고친 것이 사라진다.
    """
    version: guides.GuideVersion | None
    try:
        version = guides.current(conn)
    except GuideError as exc:
        version = None
        error = error or {"reason": exc.reason, "message": str(exc)}
    shown = draft or (version.guide if version is not None else guides.DEFAULT_GUIDE)
    return render(
        request,
        "fragments/job_analysis_form.html",
        guide=shown,
        sections=guides.SECTIONS,
        version=version,
        dirty=draft is not None
        and (version is None or guides.to_json(draft) != guides.to_json(version.guide)),
        history=guides.history(conn),
        choices=_choices(conn),
        usage=_usage(conn),
        guide_tokens=_guide_tokens(shown),
        chars_per_token=CHARS_PER_TOKEN,
        limits={
            "system": guides.MAX_SYSTEM_CHARS,
            "section": guides.MAX_SECTION_CHARS,
            "files": guides.MAX_FILES,
            "file": guides.MAX_FILE_CHARS,
            "files_total": guides.MAX_FILES_TOTAL_CHARS,
            "note": guides.MAX_NOTE_CHARS,
        },
        suffixes=", ".join(guides.FILE_SUFFIXES),
        message=message,
        error=error,
    )


@router.get("/ui/job-analysis", response_class=HTMLResponse)
def job_analysis_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> HTMLResponse:
    return _form(request, conn)


@router.put("/ui/job-analysis", response_class=HTMLResponse)
async def save_job_analysis_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> HTMLResponse:
    """새 판으로 저장한다. 이미 만든 분석은 공고 본문이 바뀌거나 다시 분석할 때 새 방법을 쓴다."""
    form = await request.form()
    try:
        draft = await _read(form)
    except GuideError as exc:
        draft, _ = parse_form(form)
        return _form(request, conn, draft=draft, error={"reason": exc.reason, "message": str(exc)})
    note = form.get("note")
    try:
        saved = guides.save(conn, draft, note if isinstance(note, str) else "")
    except GuideError as exc:
        return _form(request, conn, draft=draft, error={"reason": exc.reason, "message": str(exc)})
    return _form(
        request,
        conn,
        message=f"판 {saved.number} 로 저장했어요. 다음 분석부터 이 방법을 써요",
    )


@router.post("/ui/job-analysis/{number}/restore", response_class=HTMLResponse)
def restore_job_analysis_fragment(
    request: Request,
    number: int,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> HTMLResponse:
    """옛 판을 복사해 새 판으로 저장한다. 옛 판은 이력에 그대로 남는다."""
    try:
        saved = guides.restore(conn, number)
    except GuideError as exc:
        return _form(request, conn, error={"reason": exc.reason, "message": str(exc)})
    return _form(request, conn, message=f"판 {number} 을 판 {saved.number} 로 복사해 되돌렸어요")


@router.post("/ui/job-analysis/try", response_class=HTMLResponse)
async def try_job_analysis_fragment(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    client: Annotated[Any | None, Depends(get_analysis_client)],
    base_settings: Annotated[Settings | None, Depends(get_base_settings)],
) -> HTMLResponse:
    """폼의 방법으로 공고 하나를 분석해 오공고 화면 짜임으로 그린다. 결과는 저장하지 않는다."""
    form = await request.form()
    try:
        draft = await _read(form)
        guides.validate(draft)
        posting = _posting(conn, form)
    except GuideError as exc:
        return _trial_error(request, exc.reason, str(exc))

    trials = [await _run(conn, posting, draft, "고친 방법", None, client, base_settings)]
    if form.get("compare"):
        try:
            version = guides.current(conn)
        except GuideError as exc:
            trials.append(Trial("지금 판", None, {"reason": exc.reason, "message": str(exc)}))
        else:
            trials.append(
                await _run(
                    conn,
                    posting,
                    version.guide,
                    f"지금 판 {version.number}",
                    version.number,
                    client,
                    base_settings,
                )
            )
    return render(
        request,
        "fragments/job_analysis_try.html",
        posting=posting,
        trials=trials,
        employment_keys=EMPLOYMENT_KEYS,
        submission_keys=SUBMISSION_KEYS,
        error=None,
    )


async def _run(
    conn: sqlite3.Connection,
    posting: Posting,
    guide: Guide,
    label: str,
    version: int | None,
    client: Any | None,
    base_settings: Settings | None,
) -> Trial:
    try:
        result = await analyze(
            conn, posting, guide, settings=base_settings, client=client, guide_version=version
        )
    except AnalysisError as exc:
        return Trial(label, None, {"reason": exc.reason, "message": str(exc)})
    return Trial(label, result)


def _posting(conn: sqlite3.Connection, form: FormData) -> Posting:
    """시험할 공고. 수집한 공고를 고르거나 원문을 붙여 넣는다."""
    if form.get("source") == "paste":
        body = _text(form.get("paste_body"))
        if not body:
            raise GuideError("no_posting", "붙여 넣은 원문이 비었어요")
        return Posting(title=_text(form.get("paste_title")), body=body)
    raw = _text(form.get("job_id"))
    if not raw.isdigit():
        raise GuideError("no_posting", "시험할 공고를 고르지 않았어요")
    row = conn.execute("SELECT * FROM normalized_jobs WHERE id = ?", (int(raw),)).fetchone()
    if row is None:
        raise GuideError("not_found", f"공고 {raw} 이 없어요")
    return Posting.of(payload(row))


def _trial_error(request: Request, reason: str, message: str) -> HTMLResponse:
    return render(
        request,
        "fragments/job_analysis_try.html",
        trials=[],
        error={"reason": reason, "message": message},
    )


def _text(value: Any) -> str:
    """폼 값 하나. 브라우저가 보내는 CRLF 를 줄바꿈 하나로 맞춘다."""
    return value.replace("\r\n", "\n").strip() if isinstance(value, str) else ""


def _raw(value: Any) -> str:
    """파일 내용. 앞뒤 공백도 파일의 일부라 줄바꿈만 맞춘다."""
    return value.replace("\r\n", "\n") if isinstance(value, str) else ""
