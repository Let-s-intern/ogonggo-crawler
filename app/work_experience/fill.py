"""미래내일 일경험 프로그램 한 건에서 오공고 채용공고의 판정 칸과 글 칸을 AI 로 채운다.

2026-10-02 결정, LC-3432.

회사·제목·지역·모집인원·모집기간은 목록 카드와 상세 표에 정해진 자리가 있어 파서가 읽는다
(`app/work_experience/portal.py`). AI 가 채우는 것은 글을 읽어야 정해지는 것이다.

| 칸 | 어디서 |
|---|---|
| 직군·직무 `job_field`·`job_role` | 크롤러 직무 분류표(`job_taxonomy`)에서 고른다 |
| 산업 `industry` | 크롤러 산업 분류표에서 고른다 |
| 경력 구분·학력 `experience_type`·`education_level` | 선발기준. 없으면 경력 무관·학력 무관 |
| 시·군·구 `sub_region` | 일하는 곳이 적혀 있을 때만. 시·도는 카드의 지역으로 정해져 있다 |
| 글 칸 여덟 개 | 주요내용·모집내용·선발기준·사전직무교육을 공고 칸에 나눠 옮긴다 |

공고 분류(`app/classify/`)를 쓰지 않는 것은 그쪽이 수집 원문(`raw_jobs`)과 줄 번호 근거에 묶여
있어서다. 여기서는 프로그램 한 건이 행 하나이고, 표 칸이 이미 나뉘어 있어 한 번 부르면 된다.
제공자와 모델은 설정 > AI 의 '공고 분류' 가 고른 것을 쓰고, 비용 기록은 `work_experience_fill` 로
따로 남는다.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ValidationError, create_model

from app import industries, regions, taxonomy
from app.classify.schema import EDUCATION_LEVELS, EXPERIENCE_TYPES
from app.config import Settings
from app.llm import settings as llm_settings
from app.llm.base import LlmCallError, Usage
from app.llm.log import CLASSIFY, record_call
from app.llm.providers import for_feature
from app.work_experience.portal import Program

logger = logging.getLogger(__name__)

# `llm_calls.feature` 에 남는 이름. 제공자를 고르는 기능은 아니라 `FEATURES` 에 넣지 않는다
WORK_EXPERIENCE_FILL = "work_experience_fill"

# 공고 칸 이름. 오공고 `Job` 의 글 칸과 같은 순서다 (`app/deliver/spring.py`)
CONTENT_FIELDS: tuple[str, ...] = (
    "company_and_team_introduction",
    "responsibilities",
    "qualifications",
    "preferred_qualifications",
    "compensation",
    "benefits",
    "hiring_process",
    "recruitment_notice",
)
# 프롬프트에 보내지 않는 표 칸. 오공고에 쓸 데가 없거나 다른 자리에서 이미 보낸다
_SKIPPED: frozenset[tuple[str, str]] = frozenset(
    {
        ("사업정보", "사업명"),
        ("사업정보", "주관기관"),
        ("사업정보", "사업일정"),
        ("운영기관담당자정보", "첨부파일"),
    }
)
# 한 칸이 이보다 길면 자른다. 운영계획서를 통째로 붙인 프로그램이 있어도 호출 하나가 한없이
# 커지지 않게
MAX_VALUE_CHARS = 4000

INSTRUCTION = (
    "너는 청년 일경험 프로그램 모집 안내를 읽고 채용공고의 정해진 칸을 채운다. "
    "안내에 적힌 것만 옮기고, 적혀 있지 않은 것은 비운다. 없는 사실을 지어내지 않는다."
)

PROMPT = """아래는 고용노동부 미래내일 일경험(청년 일경험 지원사업) 프로그램 하나다.
청년이 신청해 참여기업에서 몇 주 동안 일해 보는 프로그램이고, 채용공고로 소개한다.
표의 칸 이름과 값을 그대로 준다.

고르는 칸:
- job_field / job_role: 참여자가 하게 될 일로 직군과 그 직군 밑의 직무를 아래 목록에서 고른다.
  job_role 은 반드시 고른 job_field 줄에 적힌 것 중에서 고른다.
- industry: 참여기업이 속한 산업을 아래 목록에서 고른다.
- experience_type: 경력을 요구하지 않으면 IRRELEVANT, 신입만 받는다고 적혀 있으면 NEWCOMER.
  선택지: {experience_types}
- education_level: 선발기준에 학력 조건이 적혀 있으면 그 학력, 없으면 ANY.
  선택지: {education_levels}
- sub_region: 참여자가 일하는 곳(참여기업 소재지·근무지)이 적혀 있으면 그곳의 시·군·구를
  아래 목록에서 고른다. 사전직무교육 장소는 일하는 곳이 아니다. 모르면 빈 글자.

옮기는 칸 (안내의 문장을 되도록 그대로 옮긴다. 소제목은 `■ `, 항목은 `- ` 로 시작한다.
없으면 빈 글자):
- company_and_team_introduction: 참여기업과 일하는 부서 소개
- responsibilities: 참여자가 하는 일, 프로젝트 내용, 일경험 구성
- qualifications: 지원 자격과 선발 기준. 지원 제외 대상도 여기
- preferred_qualifications: 우대사항
- compensation: 참여수당·급여처럼 참여자가 받는 돈
- benefits: 수료증·멘토링·교육 등 참여자가 얻는 것
- hiring_process: 선발 절차와 일정, 합격 발표
- recruitment_notice: 사전직무교육 안내, 제출 서류, 그 밖의 유의사항

직무 분류:
{taxonomy}

산업:
{industries}

시·군·구 ({region}):
{sub_regions}

프로그램:
{program}
"""


class ProgramFillError(RuntimeError):
    """AI 답을 쓸 수 없다. 그 프로그램은 다음 수집에서 다시 채운다."""


@dataclass(frozen=True)
class ProgramFill:
    """AI 가 채운 값. 목록 밖 값은 이미 걸렀다 — 빈 글자는 오공고에 null 로 간다."""

    job_field: str = ""
    job_role: str = ""
    industry: str = ""
    experience_type: str = "IRRELEVANT"
    education_level: str = "ANY"
    sub_region: str = ""
    company_and_team_introduction: str = ""
    responsibilities: str = ""
    qualifications: str = ""
    preferred_qualifications: str = ""
    compensation: str = ""
    benefits: str = ""
    hiring_process: str = ""
    recruitment_notice: str = ""


def region_name(label: str) -> str | None:
    """포털 지역 이름을 오공고 시·도 이름으로. `지역무관` 은 전국이다. 모르는 이름은 None 이다."""
    cleaned = label.strip()
    if cleaned == "지역무관":
        return "NATIONWIDE"
    return next((name for name, shown in regions.labels().items() if shown == cleaned), None)


def program_text(program: Program) -> str:
    """AI 에게 주는 프로그램 글. 표마다 `[표 이름]` 을 두고 `칸: 값` 으로 적는다."""
    blocks: list[str] = []
    for section, cells in program.sections.items():
        lines = [
            f"{label}: {value[:MAX_VALUE_CHARS]}"
            for label, value in cells.items()
            if value.strip() and (section, label) not in _SKIPPED
        ]
        if lines:
            blocks.append(f"[{section}]\n" + "\n".join(lines))
    return "\n\n".join(blocks)


def response_model(
    tree: list[tuple[str, tuple[str, ...]]], industry_names: tuple[str, ...], subs: tuple[str, ...]
) -> type[BaseModel]:
    """이번 호출의 응답 모양. 목록이 빈 칸은 모델에 넣지 않는다 — 고를 것 없이 채우라고 않는다."""
    fields: dict[str, Any] = {
        "experience_type": (Literal[tuple(EXPERIENCE_TYPES)], ...),
        "education_level": (Literal[tuple(EDUCATION_LEVELS)], ...),
        **{name: (str, "") for name in CONTENT_FIELDS},
    }
    if tree:
        fields["job_field"] = (Literal[tuple(major for major, _ in tree)], ...)
        minors = tuple(dict.fromkeys(minor for _, names in tree for minor in names))
        if minors:
            fields["job_role"] = (Literal[minors], ...)
    if industry_names:
        fields["industry"] = (Literal[industry_names], ...)
    if subs:
        # 목록으로 걸지 않는다. 모를 때 비울 빈 글자를 Gemini 가 enum 값으로 받지 않는다(400).
        # 시·도 밖이나 목록 밖 값은 `settle` 이 버린다
        fields["sub_region"] = (str, "")
    model: type[BaseModel] = create_model("WorkExperienceFill", **fields)
    return model


def settle(
    answer: dict[str, Any], tree: list[tuple[str, tuple[str, ...]]], region: str | None
) -> ProgramFill:
    """AI 답을 오공고가 받는 모양으로. 직무가 고른 직군 밑에 없으면 그 직군의 `기타` 직무다."""
    minors_of = dict(tree)
    field = str(answer.get("job_field") or "")
    role = str(answer.get("job_role") or "")
    if field not in minors_of:
        field, role = "", ""
    elif role not in minors_of[field]:
        role = taxonomy.etc_role(minors_of[field]) if minors_of[field] else ""
    sub = str(answer.get("sub_region") or "")
    if not region or not regions.fits(region, sub):
        sub = ""
    return ProgramFill(
        job_field=field,
        job_role=role,
        industry=str(answer.get("industry") or ""),
        experience_type=str(answer.get("experience_type") or "IRRELEVANT"),
        education_level=str(answer.get("education_level") or "ANY"),
        sub_region=sub,
        **{name: str(answer.get(name) or "").strip() for name in CONTENT_FIELDS},
    )


class LlmProgramFiller:
    """'공고 분류' 기능이 고른 제공자로 프로그램 한 건을 채운다. 호출은 `llm_calls` 에 남는다."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        settings: Settings | None = None,
        client: Any | None = None,
    ) -> None:
        self._conn = conn
        self._settings = settings
        self._client = client

    async def fill(self, program: Program) -> ProgramFill:
        tree = taxonomy.enabled_tree(self._conn)
        industry_names = industries.enabled_names(self._conn)
        region = region_name(program.region)
        subs = regions.subs_of().get(region or "", ())
        model = response_model(tree, industry_names, subs)
        prompt = PROMPT.format(
            experience_types=", ".join(EXPERIENCE_TYPES),
            education_levels=", ".join(EDUCATION_LEVELS),
            taxonomy="\n".join(f"- {major}: {', '.join(minors)}" for major, minors in tree)
            or "(없음)",
            industries="\n".join(f"- {name}" for name in industry_names) or "(없음)",
            region=program.region or "모름",
            sub_regions="\n".join(f"- {sub}" for sub in subs) or "(없음)",
            program=program_text(program),
        )

        resolved = llm_settings.settings_for(self._conn, CLASSIFY, self._settings)
        provider, model_name = for_feature(CLASSIFY, resolved)
        try:
            client = self._client or provider.build_client(resolved)
            text, usage = await provider.call_model(
                client,
                model_name,
                prompt,
                1,
                "미래내일 일경험 정리",
                response_schema=model,
                system_instruction=INSTRUCTION,
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
                self._conn,
                feature=WORK_EXPERIENCE_FILL,
                usage=failed,
                ok=False,
                error=f"{exc.reason}: {exc}",
            )
            raise ProgramFillError(f"AI 호출이 실패했다: {exc}") from exc
        record_call(self._conn, feature=WORK_EXPERIENCE_FILL, usage=usage)
        try:
            answer = model.model_validate_json(text)
        except ValidationError as exc:
            raise ProgramFillError(f"AI 답을 읽을 수 없다: {exc}") from exc
        return settle(answer.model_dump(), tree, region)
