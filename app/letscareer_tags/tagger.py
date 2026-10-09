"""렛츠커리어 콘텐츠 한 건으로 모델을 불러 추천 태그를 고른다.

제공자와 모델은 공고 분석과 같이 설정 > AI 의 '공고 분류' 가 고른 것을 쓰고, 비용 기록은
`letscareer_tags` 로 따로 남긴다.

직군·직무 이름은 오공고 enum 그대로다(`seeds/job-roles-ogonggo-*.json`). 준비 단계는 오공고
`LetsCareerContentTopic` 과 같아야 한다 — 다르면 오공고가 400 으로 거절한다. 모델이 목록 밖 이름을
내면 그 이름만 버린다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from functools import cache
from typing import Any

from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.job_roles import _seed as job_role_seed
from app.letscareer_tags.ogonggo import Target
from app.llm import settings as llm_settings
from app.llm.base import LlmCallError, Usage
from app.llm.log import CLASSIFY, record_call
from app.llm.providers import for_feature

# `llm_calls.feature` 에 남는 이름
LETSCAREER_TAGS = "letscareer_tags"
MAX_ATTEMPTS = 2
MAX_TOPICS = 3

# 오공고 `LetsCareerContentTopic` 과 같은 순서·이름
TOPICS: dict[str, str] = {
    "CAREER_START": "취업 준비 시작(취준 입문, 일정·전략, 직무 탐색)",
    "RESUME": "이력서",
    "PERSONAL_STATEMENT": "자기소개서",
    "PORTFOLIO": "포트폴리오",
    "INTERVIEW": "면접",
    "WRITTEN_TEST": "인적성·필기·코딩테스트",
    "EXPERIENCE_SUMMARY": "경험 정리",
    "INDUSTRY_COMPANY_ANALYSIS": "산업·기업 분석",
    "LARGE_COMPANY": "대기업 준비",
    "INTERNSHIP": "인턴",
}

KIND_LABELS: dict[str, str] = {
    "CHALLENGE": "챌린지(여러 주 동안 함께하는 온라인 프로그램)",
    "LIVE": "라이브 클래스",
    "VOD": "VOD 강의",
    "GUIDEBOOK": "가이드북",
    "REPORT": "서류 진단(첨삭)",
    "MATERIAL": "무료 자료집",
    "BLOG": "블로그 글",
}

SYSTEM = (
    "너는 취업 준비 서비스의 콘텐츠를 채용공고에 연결하는 분류 담당이다. "
    "확실한 것만 태그하고, 애매하면 비운다."
)

_SKELETON = """# 할 일

렛츠커리어(신입·인턴 취업 준비 서비스)의 콘텐츠 하나를 읽고, 채용공고 사이트 오공고가
어느 공고 상세에서 이 콘텐츠를 추천할지 고를 수 있도록 태그를 붙인다.

# 규칙

- job_fields: 특정 직군 지원자를 위한 콘텐츠일 때만 그 직군 이름을 쓴다. 자기소개서 작성법,
  면접 일반, 취업 일정처럼 직군과 상관없이 쓰는 콘텐츠는 빈 배열이다. 여러 직군에 맞으면 모두
  쓴다(최대 5개).
- job_roles: 직군 안에서도 특정 직무만을 위한 것일 때만 쓴다. 직군 전체에 맞으면 빈 배열이다.
- topics: 콘텐츠가 다루는 취업 준비 단계. 1개 이상 {max_topics}개 이하.
- 이름은 아래 목록의 영문 이름을 그대로 쓴다. 목록에 없는 이름은 쓰지 않는다.

# 직군과 직무 (영문 이름: 한글 이름)
{taxonomy}
# 준비 단계
{topics}
# 콘텐츠

{content}
"""


class TagAnswer(BaseModel):
    job_fields: list[str]
    job_roles: list[str]
    topics: list[str]


@dataclass(frozen=True)
class TagResult:
    tags: dict[str, list[str]]
    model: str
    usages: tuple[Usage, ...]
    dropped: tuple[str, ...] = ()


class TagError(RuntimeError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@cache
def _taxonomy() -> tuple[dict[str, str], dict[str, str]]:
    """(직군 이름 → 한글, 직무 이름 → 한글)."""
    fields: dict[str, str] = {}
    roles: dict[str, str] = {}
    for item in job_role_seed():
        fields[str(item["name"])] = str(item["label"])
        for role in item["roles"]:
            roles[str(role["name"])] = str(role["label"])
    return fields, roles


@cache
def _taxonomy_text() -> str:
    lines: list[str] = []
    for item in job_role_seed():
        roles = ", ".join(f"{role['name']}: {role['label']}" for role in item["roles"])
        lines.append(f"\n## {item['name']}: {item['label']}\n\n{roles}\n")
    return "".join(lines)


def build_prompt(target: Target) -> str:
    content = [
        f"- 종류: {KIND_LABELS.get(target.kind, target.kind)}",
        f"- 분류: {target.category or '(없음)'}",
        f"- 제목: {target.title}",
        f"- 설명: {target.description or '(없음)'}",
        f"- 단서: {', '.join(target.labels) or '(없음)'}",
    ]
    return _SKELETON.format(
        max_topics=MAX_TOPICS,
        taxonomy=_taxonomy_text(),
        topics="".join(f"\n- {name}: {label}" for name, label in TOPICS.items()) + "\n",
        content="\n".join(content),
    )


def settle(answer: TagAnswer) -> tuple[dict[str, list[str]], tuple[str, ...]]:
    """목록 밖 이름을 버리고 오공고가 받는 모양으로 만든다. 버린 이름도 돌려준다."""
    fields, roles = _taxonomy()
    dropped: list[str] = []

    def keep(names: list[str], allowed: dict[str, str], limit: int) -> list[str]:
        kept: list[str] = []
        for name in names:
            cleaned = name.strip()
            if cleaned in allowed and cleaned not in kept:
                kept.append(cleaned)
            elif cleaned:
                dropped.append(cleaned)
        return kept[:limit]

    tags = {
        "jobFields": keep(answer.job_fields, fields, 5),
        "jobRoles": keep(answer.job_roles, roles, 20),
        "topics": keep(answer.topics, TOPICS, MAX_TOPICS),
    }
    return tags, tuple(dropped)


async def tag(
    conn: sqlite3.Connection,
    target: Target,
    *,
    settings: Settings | None = None,
    client: Any | None = None,
) -> TagResult:
    """콘텐츠 한 건에 태그를 붙인다. 호출은 성공·실패 모두 `llm_calls` 에 남는다."""
    if not target.title.strip():
        raise TagError("empty_title", "제목이 비어 태그할 것이 없다")
    resolved = llm_settings.settings_for(conn, CLASSIFY, settings)
    provider, model_name = for_feature(CLASSIFY, resolved)
    prompt = build_prompt(target)
    usages: list[Usage] = []
    retry = ""
    try:
        built = client or provider.build_client(resolved)
    except LlmCallError as exc:
        raise TagError(exc.reason, f"AI 를 부를 수 없다: {exc}") from exc

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            text, usage = await provider.call_model(
                built,
                model_name,
                prompt + retry,
                attempt,
                "렛츠커리어 콘텐츠 태그",
                response_schema=TagAnswer,
                system_instruction=SYSTEM,
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
                conn, feature=LETSCAREER_TAGS, usage=failed, ok=False, error=f"{exc.reason}: {exc}"
            )
            raise TagError(exc.reason, f"AI 호출이 실패했다: {exc}") from exc
        usages.append(usage)
        record_call(conn, feature=LETSCAREER_TAGS, usage=usage)
        try:
            answer = TagAnswer.model_validate_json(text)
        except ValidationError as exc:
            if attempt == MAX_ATTEMPTS:
                raise TagError("unparsable_answer", f"AI 답을 읽을 수 없다: {exc}") from exc
            retry = (
                "\n# 앞선 답을 받지 않았다\n\n답이 정해진 모양이 아니었다.\n\n"
                f"{str(exc)[:1000]}\n\n처음부터 다시 답한다.\n"
            )
            continue
        tags, dropped = settle(answer)
        return TagResult(tags=tags, model=model_name, usages=tuple(usages), dropped=dropped)
    raise AssertionError("다시 묻기 횟수 안에서 돌려주거나 실패해야 한다")
