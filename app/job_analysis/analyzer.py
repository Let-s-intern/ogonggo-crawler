"""분석 방법과 공고 한 건으로 모델을 불러 공고 분석을 만든다.

제공자와 모델은 설정 > AI 의 '공고 분류' 가 고른 것을 쓴다. 비용 기록은 `job_analysis` 로 따로
남긴다 — 분류와 같은 모델을 써도 무엇이 토큰을 썼는지는 갈라 세야 한다.

## 프롬프트의 짜임

시스템 지시는 화면의 '시스템 지시' 그대로다. 사용자 프롬프트는 코드의 골격(할 일, 바꿀 수 없는 규칙,
답의 칸) 사이에 화면의 항목별 지침과 참고 파일을 끼우고, 맨 끝에 공고를 둔다. 골격이 화면에서
바뀌면 답이 오공고가 그리는 모양과 어긋난다.

## 다시 묻기

답이 모양에 맞지 않거나 '공고 속 문구' 를 원문에서 찾지 못하면 무엇이 틀렸는지 적어 한 번 더 묻는다.
두 번째에도 문구를 못 찾은 역량은 빼고 나머지를 쓴다 — 역량 하나 때문에 분석 전체를 버리지 않는다.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.config import Settings
from app.job_analysis.guide import SECTIONS, Guide
from app.job_analysis.posting import Posting
from app.job_analysis.schema import (
    EMPLOYMENT_KEYS,
    MAX_COMPETENCIES,
    MAX_CONDITIONS,
    MAX_EXPERIENCES,
    MAX_TASKS,
    SUBMISSION_KEYS,
    AnalysisAnswer,
    settle,
)
from app.llm import settings as llm_settings
from app.llm.base import LlmCallError, Usage
from app.llm.log import CLASSIFY, record_call
from app.llm.providers import for_feature

# `llm_calls.feature` 에 남는 이름. 제공자를 고르는 기능은 아니라 `FEATURES` 에 넣지 않는다
JOB_ANALYSIS = "job_analysis"
MAX_ATTEMPTS = 2

_SKELETON = """# 할 일

아래 채용공고를 읽고 지원자에게 보여 줄 공고 분석을 만든다. 답은 정해진 모양으로만 낸다.

# 바꿀 수 없는 규칙

- 공고에 적힌 것만 쓴다. 공고에서 확인할 수 없는 값은 value 를 빈 글자로 둔다. 빈 값은 화면에
  '공고에 명시 없음' 으로 나온다. 그럴듯한 값을 채워 넣지 않는다.
- competencies 의 quote 는 공고 문장을 한 글자도 바꾸지 않고 그대로 옮긴다. 줄이려면 앞뒤를 자르되
  가운데를 고치지 않는다.
- 개수: tasks 는 {max_tasks}개까지, required·preferred 는 각각 {max_conditions}개까지,
  competencies 는 {max_competencies}개까지, 역량마다 experiences 는 {max_experiences}개다.
- employment 의 칸: {employment_keys}. submission 의 칸: {submission_keys}.
  칸마다 value(본 값)와 note(짧은 보충, 없으면 빈 글자)를 쓴다.

# 항목별 지침
{sections}{files}
# 공고

{posting}
"""


@dataclass(frozen=True)
class AnalysisResult:
    """분석 한 번의 결과. 화면은 프롬프트와 토큰까지 보여 준다."""

    analysis: dict[str, Any]
    model: str
    prompt: str
    system: str
    usages: tuple[Usage, ...]
    dropped: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default=())

    @property
    def input_tokens(self) -> int:
        return sum(usage.input_tokens for usage in self.usages)

    @property
    def output_tokens(self) -> int:
        return sum(usage.output_tokens for usage in self.usages)


class AnalysisError(RuntimeError):
    """분석을 만들지 못했다. `reason` 을 화면이 그대로 적는다."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def build_prompt(guide: Guide, posting: Posting) -> str:
    """분석 방법과 공고로 사용자 프롬프트를 만든다. 화면의 '프롬프트 보기' 도 이 글이다."""
    sections = "".join(
        f"\n## {label} ({fields})\n\n{guide.sections.get(key, '').strip() or '(지침 없음)'}\n"
        for key, label, fields in SECTIONS
    )
    files = "".join(f"\n## {item.name}\n\n{item.content.strip()}\n" for item in guide.files)
    return _SKELETON.format(
        max_tasks=MAX_TASKS,
        max_conditions=MAX_CONDITIONS,
        max_competencies=MAX_COMPETENCIES,
        max_experiences=MAX_EXPERIENCES,
        employment_keys=", ".join(f"{key}({label})" for key, label in EMPLOYMENT_KEYS),
        submission_keys=", ".join(f"{key}({label})" for key, label in SUBMISSION_KEYS),
        sections=sections,
        files=f"\n# 참고 자료\n{files}" if files else "",
        posting=posting.text(),
    )


async def analyze(
    conn: sqlite3.Connection,
    posting: Posting,
    guide: Guide,
    *,
    settings: Settings | None = None,
    client: Any | None = None,
    guide_version: int | None = None,
    on_call: Callable[[Usage], None] | None = None,
) -> AnalysisResult:
    """공고 한 건을 분석한다. 호출은 성공·실패 모두 `llm_calls` 에 남는다.

    `guide_version` 은 저장한 판으로 돌렸을 때 그 번호다. 저장하지 않은 방법으로 돌린 시험은 None
    이다.
    """
    if posting.empty:
        raise AnalysisError("empty_body", "공고 본문이 비어 분석할 것이 없다")
    resolved = llm_settings.settings_for(conn, CLASSIFY, settings)
    provider, model_name = for_feature(CLASSIFY, resolved)
    prompt = build_prompt(guide, posting)
    source = posting.source()
    usages: list[Usage] = []
    retry = ""
    try:
        built = client or provider.build_client(resolved)
    except LlmCallError as exc:
        raise AnalysisError(exc.reason, f"AI 를 부를 수 없다: {exc}") from exc

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            text, usage = await provider.call_model(
                built,
                model_name,
                prompt + retry,
                attempt,
                "공고 분석",
                response_schema=AnalysisAnswer,
                system_instruction=guide.system,
            )
        except LlmCallError as exc:
            failed = Usage(
                provider=provider.name,
                model=model_name,
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                latency_ms=0,
            )
            record_call(
                conn,
                feature=JOB_ANALYSIS,
                usage=failed,
                ok=False,
                error=f"{exc.reason}: {exc}",
                rules_version=guide_version,
            )
            raise AnalysisError(exc.reason, f"AI 호출이 실패했다: {exc}") from exc
        usages.append(usage)
        record_call(conn, feature=JOB_ANALYSIS, usage=usage, rules_version=guide_version)
        if on_call is not None:
            on_call(usage)

        try:
            answer = AnalysisAnswer.model_validate_json(text)
        except ValidationError as exc:
            if attempt == MAX_ATTEMPTS:
                raise AnalysisError("unparsable", f"AI 답을 읽을 수 없다: {exc}") from exc
            retry = _retry_block("답이 정해진 모양이 아니었다", str(exc)[:1500])
            continue

        settled = settle(answer, source)
        if settled.ungrounded and attempt < MAX_ATTEMPTS:
            retry = _retry_block(
                "아래 역량의 quote 를 공고에서 찾지 못했다. 공고 문장을 그대로 옮긴다",
                "\n".join(f"- {name}" for name in settled.ungrounded),
            )
            continue
        notes = (
            (f"공고에서 문구를 찾지 못해 뺀 역량: {', '.join(settled.ungrounded)}",)
            if settled.ungrounded
            else ()
        )
        return AnalysisResult(
            analysis=settled.analysis,
            model=model_name,
            prompt=prompt,
            system=guide.system,
            usages=tuple(usages),
            dropped=settled.ungrounded,
            notes=notes,
        )
    raise AssertionError("다시 묻기 횟수 안에서 돌려주거나 실패해야 한다")


def _retry_block(reason: str, detail: str) -> str:
    return f"\n# 앞선 답을 받지 않았다\n\n{reason}.\n\n{detail}\n\n처음부터 다시 답한다.\n"
