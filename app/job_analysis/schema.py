"""AI 답의 모양과, 답을 오공고가 받는 분석으로 다듬기.

## 화면에서 바꿀 수 없는 것

항목의 이름과 개수는 오공고 서버와 프런트가 그대로 그리는 약속이라 코드에 둔다. 분석 방법 화면에서
바꾸는 것은 각 항목을 **어떻게** 쓰는지다 (`app/job_analysis/guide.py`).

## 빈 글자는 '공고에 명시 없음'

고용 형태·제출물 칸은 원문에서 확인할 수 없으면 값을 비우게 한다. 답의 모양에서 null 을 쓰지 않는
것은 strict 도구로 답하게 하는 제공자가 null 과 글자를 섞은 칸을 다르게 다뤄서다. 다듬을 때 빈
글자를
null 로 바꾸고, 프런트는 null 을 '공고에 명시 없음' 으로 그린다.

## 공고 속 문구는 원문에 있어야 한다

역량의 `quote` 는 사용자에게 '공고 속 문구' 로 보인다. 원문에 없는 문장이 따옴표 안에 들어가면
공고가
하지 않은 말을 공고가 했다고 보여 주는 셈이라, 띄어쓰기와 따옴표·글머리 기호를 뺀 글자로 원문에서
찾는다. 못 찾은 역량은 한 번 다시 묻고, 그래도 못 찾으면 그 역량을 뺀다
(`app/job_analysis/analyzer.py`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

# 개수 상한. 모델이 더 내면 앞에서부터 이만큼만 쓴다
MAX_TASKS = 3
MAX_CONDITIONS = 8
MAX_COMPETENCIES = 3
MAX_EXPERIENCES = 3
# 항목 하나의 길이 상한. 넘으면 자른다 — 카드 한 줄에 들어가지 않는 글은 이미 요약이 아니다
MAX_ITEM_CHARS = 400

EMPLOYMENT_KEYS: tuple[tuple[str, str], ...] = (
    ("type", "형태"),
    ("conversion", "전환"),
    ("salary", "급여"),
    ("affiliation", "소속"),
)
SUBMISSION_KEYS: tuple[tuple[str, str], ...] = (
    ("documents", "제출"),
    ("essay", "자소서"),
    ("process", "전형"),
    ("deadline", "마감"),
)


class AnalysisTask(BaseModel):
    tag: str
    text: str


class AnalysisFact(BaseModel):
    value: str
    note: str


class AnalysisEmployment(BaseModel):
    type: AnalysisFact
    conversion: AnalysisFact
    salary: AnalysisFact
    affiliation: AnalysisFact


class AnalysisSubmission(BaseModel):
    documents: AnalysisFact
    essay: AnalysisFact
    process: AnalysisFact
    deadline: AnalysisFact


class AnalysisCompetency(BaseModel):
    name: str
    quote: str
    description: str
    experiences: list[str]


class AnalysisAnswer(BaseModel):
    """AI 가 내는 답.

    개수와 길이 제한은 스키마에 걸지 않는다 — strict 도구가 받지 않는 제공자가 있다.
    """

    tasks: list[AnalysisTask]
    required: list[str]
    preferred: list[str]
    employment: AnalysisEmployment
    submission: AnalysisSubmission
    competencies: list[AnalysisCompetency]


@dataclass(frozen=True)
class Settled:
    """다듬은 분석과, 원문에서 찾지 못한 역량 이름."""

    analysis: dict[str, Any]
    ungrounded: tuple[str, ...]


def settle(answer: AnalysisAnswer, source: str) -> Settled:
    """AI 답을 오공고가 받는 모양으로. 원문에서 찾지 못한 `quote` 의 역량은 빼고 이름을 돌려준다."""
    haystack = squash(source)
    competencies: list[dict[str, Any]] = []
    ungrounded: list[str] = []
    for item in answer.competencies:
        name = _clip(item.name)
        quote = _clip(item.quote)
        if not name:
            continue
        if not quote or squash(quote) not in haystack:
            ungrounded.append(name)
            continue
        competencies.append(
            {
                "name": name,
                "quote": quote,
                "description": _clip(item.description),
                "experiences": _list(item.experiences, MAX_EXPERIENCES),
            }
        )
    analysis = {
        "tasks": [
            {"tag": _clip(task.tag), "text": _clip(task.text)}
            for task in answer.tasks
            if task.text.strip()
        ][:MAX_TASKS],
        "required": _list(answer.required, MAX_CONDITIONS),
        "preferred": _list(answer.preferred, MAX_CONDITIONS),
        "employment": {key: _fact(getattr(answer.employment, key)) for key, _ in EMPLOYMENT_KEYS},
        "submission": {key: _fact(getattr(answer.submission, key)) for key, _ in SUBMISSION_KEYS},
        "competencies": competencies[:MAX_COMPETENCIES],
    }
    return Settled(analysis, tuple(ungrounded))


# 원문과 문구를 견줄 때 빼는 글자. 띄어쓰기·줄바꿈, 따옴표, 글머리 기호처럼 옮기다 바뀌기 쉬운
# 것이다
_NOISE = re.compile(r"[\s\"'“”‘’`·•\-–—*■□▶▷>]+")


def squash(text: str) -> str:
    return _NOISE.sub("", text)


def _fact(fact: AnalysisFact) -> dict[str, str | None]:
    return {"value": _clip(fact.value) or None, "note": _clip(fact.note) or None}


def _list(items: list[str], limit: int) -> list[str]:
    return [text for text in (_clip(item) for item in items) if text][:limit]


def _clip(text: str) -> str:
    return " ".join(text.split())[:MAX_ITEM_CHARS]
