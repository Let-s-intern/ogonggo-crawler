"""새싹 과정 한 건에서 오공고 부트캠프의 글 칸을 AI 로 채운다 (2026-09-22 결정, LC-3364).

날짜·캠퍼스·분야·목차는 HTML 에 정해진 자리가 있어 파서가 읽는다 (`app/bootcamp/sesac.py`). AI 가
채우는 것은 교육개요에만 있는 것이다. 새싹 교육개요는 대개 이미지 몇 장이 전부라
이미지를 함께 보낸다.

| 칸 | 어디서 |
|---|---|
| 한 줄 소개 `short_description` | 교육개요를 한 문장으로 |
| 상세 내용 `content` | 교육개요에 적힌 과정 소개·교육 내용·혜택. 지어내지 않는다 |
| 지원 자격·전형 `eligibility_and_selection_process` | 교육개요에 있으면. 없으면 비운다 |
| 모집 정원 `capacity` | 교육개요에 숫자로 적혀 있으면 |
| 담당자 이메일 `manager_email` | 교육개요에 적혀 있으면 |

**호출은 한 번이다.** 이미지를 글로 옮기는 호출과 칸을 채우는 호출을 나누지 않는다. 제공자와 모델은
설정 > AI 의 '이미지 읽기' 기능이 고른 것을 쓴다 — 이미지를 받는 제공자여야 해서다. 비용 기록은
`bootcamp_fill` 로 따로 남는다.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from app.bootcamp.sesac import Course
from app.config import Settings
from app.crawler.fetcher import FetchError, FetchPolicy
from app.crawler.images import MAX_IMAGE_BYTES, ImageReadError, split_tall
from app.llm import settings as llm_settings
from app.llm.base import ImageInput, LlmCallError, Usage
from app.llm.log import IMAGE_READ, record_call
from app.llm.providers import for_feature

logger = logging.getLogger(__name__)

# `llm_calls.feature` 에 남는 이름. 제공자를 고르는 기능은 아니라 `FEATURES` 에 넣지 않는다
BOOTCAMP_FILL = "bootcamp_fill"

# 새싹 교육개요는 이미지가 한 장부터 열세 장까지다 (2026-09-22 모집 중 세 과정).
# 공고 이미지 읽기보다 넉넉히 두되, 잘못 잡힌 영역이 사진첩이어도 호출 하나가 한없이
# 커지지 않게 상한을 둔다
MAX_IMAGES = 15
MAX_TILES = 20

INSTRUCTION = (
    "너는 부트캠프(취업 교육 과정) 모집 안내를 읽고 정해진 칸을 채운다. 안내에 적힌 것만 옮기고, "
    "적혀 있지 않은 것은 비운다. 없는 사실을 지어내지 않는다."
)

PROMPT = """아래는 서울시 청년취업사관학교 새싹(SeSAC)의 오프라인 교육 과정 하나다.
과정명·기간·목차는 글로 주고, 교육개요는 글과 이미지(위에서부터 차례로 자른 조각)로 준다.

칸을 채우는 규칙:
- short_description: 과정을 한 문장(100자 이내)으로 소개한다.
  누구에게 무엇을 가르쳐 어디로 이끄는지.
- content: 교육개요에 적힌 과정 소개·교육 목표·교육 내용·혜택·특전·일정 안내를 소제목과 줄로 정리한
  글. 소제목은 `■ 과정 소개` 처럼 한 줄로 쓰고, 항목은 `- ` 로 시작한다. 이미지에 적힌 문장을 되도록
  그대로 옮기고, 요약하더라도 사실을 바꾸지 않는다. 지원 자격·선발 절차는 여기 넣지 않는다.
- eligibility_and_selection_process: 지원 자격(대상)과 선발 절차·일정. 적혀 있지 않으면 null.
- capacity: 모집 인원이 숫자로 적혀 있으면 그 숫자. 없으면 null.
- manager_email: 문의 이메일 주소가 적혀 있으면 그대로. 없으면 null.

과정 정보:
{facts}

교육개요 글:
{overview}
"""


class BootcampFillError(RuntimeError):
    """이미지를 받지 못했거나 AI 답을 쓸 수 없다. 그 과정은 다음 수집에서 다시 채운다."""


class BootcampFill(BaseModel):
    short_description: str = Field(default="")
    content: str = Field(default="")
    eligibility_and_selection_process: str | None = None
    capacity: int | None = None
    manager_email: str | None = None


def facts(course: Course) -> str:
    """AI 에게 글로 주는 과정 정보. 파서가 읽은 값 그대로다."""
    lines = [
        f"과정명: {course.title}",
        f"캠퍼스: {course.campus}",
        f"분야: {course.category}",
        f"모집기간: {course.recruitment_start or ''} ~ {course.recruitment_end or ''}",
        f"교육기간: {course.program_start or ''} ~ {course.program_end or ''}",
    ]
    if course.hours:
        lines.append(f"교육시간: {course.hours}시간")
    if course.curriculum:
        lines.append("학습목차:")
        lines.extend(f"- {group.name}" for group in course.curriculum)
    return "\n".join(lines)


async def download_images(sources: Sequence[str], fetcher: FetchPolicy) -> list[ImageInput]:
    """교육개요 이미지를 받아 조각낸다.

    한 장이라도 못 받으면 실패다 — 빠진 장의 내용이 본문에서 사라진다.
    """
    parts: list[ImageInput] = []
    for url in list(dict.fromkeys(sources))[:MAX_IMAGES]:
        result = await fetcher.request(url)
        if not result.content:
            raise ImageReadError(f"이미지가 비었다: {url}")
        if len(result.content) > MAX_IMAGE_BYTES:
            raise ImageReadError(f"이미지가 {len(result.content):,}바이트라 읽지 않는다: {url}")
        parts.extend(split_tall(result.content))
        if len(parts) >= MAX_TILES:
            break
    return parts[:MAX_TILES]


class LlmBootcampFiller:
    """'이미지 읽기' 기능이 고른 제공자로 과정 한 건의 글 칸을 채운다.

    호출은 `llm_calls` 에 남는다.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        fetcher: FetchPolicy,
        *,
        settings: Settings | None = None,
        client: Any | None = None,
    ) -> None:
        self._conn = conn
        self._fetcher = fetcher
        self._settings = settings
        self._client = client

    async def fill(self, course: Course) -> BootcampFill:
        try:
            images = await download_images(course.overview_images, self._fetcher)
        except (FetchError, ImageReadError) as exc:
            raise BootcampFillError(f"교육개요 이미지를 받지 못했다: {exc}") from exc
        if not images and not course.overview_text.strip():
            raise BootcampFillError("교육개요에 글도 이미지도 없다")

        resolved = llm_settings.settings_for(self._conn, IMAGE_READ, self._settings)
        provider, model = for_feature(IMAGE_READ, resolved)
        client = self._client or provider.build_client(resolved)
        prompt = PROMPT.format(facts=facts(course), overview=course.overview_text or "(없음)")
        try:
            text, usage = await provider.call_model(
                client,
                model,
                prompt,
                1,
                "부트캠프 정리",
                response_schema=BootcampFill,
                system_instruction=INSTRUCTION,
                images=images,
            )
        except LlmCallError as exc:
            failed = Usage(
                provider=provider.name,
                model=model,
                input_tokens=0,
                output_tokens=0,
                total_tokens=0,
                latency_ms=0,
            )
            record_call(
                self._conn,
                feature=BOOTCAMP_FILL,
                usage=failed,
                ok=False,
                error=f"{exc.reason}: {exc}",
            )
            raise BootcampFillError(f"AI 호출이 실패했다: {exc}") from exc
        record_call(self._conn, feature=BOOTCAMP_FILL, usage=usage)
        try:
            answer = BootcampFill.model_validate_json(text)
        except ValidationError as exc:
            raise BootcampFillError(f"AI 답을 읽을 수 없다: {exc}") from exc
        return clean(answer)


def clean(answer: BootcampFill) -> BootcampFill:
    """빈 글은 None 으로, 음수 정원과 `@` 없는 이메일은 버린다. 오공고가 공백 칸을 거절한다."""
    eligibility = (answer.eligibility_and_selection_process or "").strip() or None
    email = (answer.manager_email or "").strip()
    return BootcampFill(
        short_description=answer.short_description.strip()[:500],
        content=answer.content.strip(),
        eligibility_and_selection_process=eligibility,
        capacity=answer.capacity if answer.capacity is not None and answer.capacity > 0 else None,
        manager_email=email if "@" in email and len(email) <= 320 else None,
    )
