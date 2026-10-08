"""대상 직군 밖 공고를 수집에서 거른다 (2026-10-08 결정, LC-3444).

오공고에는 마케팅·인사·기획·영업·개발 공고만 올린다 (`app/crawler/target_fields.py`). 직군은 본문을
분류해야 확실히 알지만, 대상 밖 공고까지 상세를 열고 분류하면 그 비용이 그대로 나간다. 그래서 두 번
거른다.

| 단계 | 언제 | 거르는 것 |
|---|---|---|
| 제목 | 목록을 읽은 뒤, 상세를 열기 전. 새 공고 제목을 한꺼번에 | 직무가 분명히 대상 밖 |
| 본문 | 상세를 연 뒤, `raw_jobs` 에 넣기 전 | 뽑는 직무에 대상 직군이 없음 |

"2026년 BNK캐피탈 신입사원 채용" 처럼 제목으로 모르는 공고는 제목 단계에서 `unknown` 이 되어 본문
단계로 간다. 제목 단계에서 `in` 이면 본문 단계는 건너뛴다.

**애매하면 수집한다.** 대상 공고를 놓치는 쪽이 대상 밖 공고가 섞이는 쪽보다 나쁘다. 섞인 공고는
전송의 직군 조건(`app/deliver/spring.py`)이 한 번 더 막고, 그래도 올라간 것은 오공고 어드민에서
비활성화한다. AI 호출이 실패해도 수집한다.

거른 공고는 `screened_jobs` 에 남고 수집은 그 주소를 아는 공고로 본다
(`migrations/0053_screened_jobs.sql`).

제공자와 모델은 설정 > AI 의 '본문 분류' 가 고른 것을 쓴다. 비용은 `collect_screen` 으로 따로 센다.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from app import job_roles
from app.config import Settings
from app.crawler import target_fields
from app.llm import settings as llm_settings
from app.llm.base import LlmCallError, Usage
from app.llm.log import CLASSIFY, record_call
from app.llm.providers import for_feature

# `llm_calls.feature` 에 남는 이름. 제공자를 고르는 기능은 아니라 `FEATURES` 에 넣지 않는다
COLLECT_SCREEN = "collect_screen"

TITLE = "title"
BODY = "body"

IN = "in"
OUT = "out"
UNKNOWN = "unknown"

# 한 호출에 싣는 제목 수. 첫 실행은 수백 건이 한꺼번에 온다
TITLE_BATCH = 100
# 본문 단계에 싣는 본문 길이. 직무 이름은 대개 앞쪽에 있고, 긴 공고 하나가 호출을 키우지 않게 한다
MAX_BODY_CHARS = 8000
# `screened_jobs.reason` 에 남기는 길이
MAX_REASON_CHARS = 300

INSTRUCTION = (
    "너는 채용공고가 정해진 직군에 드는지 가른다. 정해진 모양으로만 답한다. "
    "확실하지 않으면 대상 밖이라고 답하지 않는다."
)

_TITLE_TASK = """# 할 일

아래 채용공고 제목마다 verdict 를 하나 고른다. index 는 제목 앞 번호 그대로다.

- in: 제목에 드러난 직무가 대상 직군에 든다
- out: 제목에 드러난 직무가 분명히 대상 직군 밖이다
- unknown: 제목만으로 직무를 알 수 없다. "신입사원 채용", "공개채용", "경력직 수시채용" 처럼 직무가
  없거나, 여러 직무를 한꺼번에 뽑는 공고다. 대상 직군이 하나라도 섞였을 수 있으면 unknown 이다

헷갈리면 out 을 고르지 않는다.
"""

_BODY_TASK = """# 할 일

아래 채용공고가 뽑는 직무에 대상 직군이 하나라도 있는지 가른다.

- in: 대상 직군 직무를 하나라도 뽑는다. 여러 직무 중 하나만 대상이어도 in 이다
- out: 뽑는 직무가 전부 분명히 대상 직군 밖이다

본문으로도 직무를 알 수 없으면 in 이다. fields 에는 공고가 뽑는 직무의 직군 이름을 적는다.
reason 에는 그렇게 판단한 근거를 한 문장으로 적는다.
"""


class TitleVerdict(BaseModel):
    index: int
    verdict: Literal["in", "out", "unknown"]


class TitleAnswer(BaseModel):
    items: list[TitleVerdict]


class BodyAnswer(BaseModel):
    verdict: Literal["in", "out"]
    fields: list[str]
    reason: str


class ScreenError(RuntimeError):
    """판정하지 못했다. 부르는 쪽은 공고를 그대로 수집한다."""


@dataclass(frozen=True)
class BodyVerdict:
    verdict: str
    reason: str


def targets_text(fields: Sequence[str]) -> str:
    """프롬프트에 싣는 대상 직군. 직군마다 딸린 직무를 같이 적어 경계를 보인다."""
    lines = [f"- {name}: {', '.join(job_roles.role_labels(name))}" for name in fields]
    return "# 대상 직군\n\n" + "\n".join(lines) + "\n"


def title_prompt(fields: Sequence[str], titles: Sequence[str]) -> str:
    listed = "\n".join(f"{index}. {title.strip()}" for index, title in enumerate(titles))
    return f"{targets_text(fields)}\n{_TITLE_TASK}\n# 제목\n\n{listed}\n"


def body_prompt(fields: Sequence[str], title: str, body: str) -> str:
    clipped = body[:MAX_BODY_CHARS]
    return f"{targets_text(fields)}\n{_BODY_TASK}\n# 공고\n\n제목: {title.strip()}\n\n{clipped}\n"


class Screener:
    """대상 직군으로 공고를 가른다. 호출은 성공·실패 모두 `llm_calls` 에 남긴다."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        fields: Sequence[str],
        *,
        settings: Settings | None = None,
        client: Any | None = None,
    ) -> None:
        self._conn = conn
        self.fields = tuple(fields)
        self._settings = settings
        self._client = client

    async def titles(self, titles: Sequence[str]) -> list[str]:
        """제목마다 `in`/`out`/`unknown`. 답에 빠진 제목은 `unknown` 이다."""
        verdicts: list[str] = []
        for start in range(0, len(titles), TITLE_BATCH):
            chunk = titles[start : start + TITLE_BATCH]
            text = await self._call(title_prompt(self.fields, chunk), TitleAnswer, "제목 거르기")
            try:
                answer = TitleAnswer.model_validate_json(text)
            except ValidationError as exc:
                raise ScreenError(f"제목 거르기 답을 읽을 수 없다: {exc}") from exc
            by_index = {item.index: item.verdict for item in answer.items}
            verdicts.extend(by_index.get(index, UNKNOWN) for index in range(len(chunk)))
        return verdicts

    async def body(self, title: str, body: str) -> BodyVerdict:
        """공고 하나가 대상 직군을 하나라도 뽑는가."""
        text = await self._call(body_prompt(self.fields, title, body), BodyAnswer, "본문 거르기")
        try:
            answer = BodyAnswer.model_validate_json(text)
        except ValidationError as exc:
            raise ScreenError(f"본문 거르기 답을 읽을 수 없다: {exc}") from exc
        fields = ", ".join(answer.fields)
        reason = f"{answer.reason.strip()} ({fields})" if fields else answer.reason.strip()
        return BodyVerdict(verdict=answer.verdict, reason=reason[:MAX_REASON_CHARS])

    async def _call(self, prompt: str, schema: type[BaseModel], kind: str) -> str:
        # 화면에서 고른 제공자와 모델을 부를 때마다 다시 읽는다 (`app/llm/settings.py`)
        try:
            resolved = llm_settings.settings_for(self._conn, CLASSIFY, self._settings)
            provider, model = for_feature(CLASSIFY, resolved)
            client = self._client or provider.build_client(resolved)
        except LlmCallError as exc:
            raise ScreenError(f"AI 를 부를 수 없다: {exc}") from exc
        try:
            text, usage = await provider.call_model(
                client,
                model,
                prompt,
                1,
                kind,
                response_schema=schema,
                system_instruction=INSTRUCTION,
            )
        except LlmCallError as exc:
            # 응답을 받지 못한 호출도 남긴다. 토큰은 알 수 없어 0 이다
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
                feature=COLLECT_SCREEN,
                usage=failed,
                ok=False,
                error=f"{exc.reason}: {exc}",
            )
            raise ScreenError(f"AI 호출이 실패했다: {exc}") from exc
        record_call(self._conn, feature=COLLECT_SCREEN, usage=usage)
        return text


def make_screener(conn: sqlite3.Connection) -> Screener | None:
    """이 실행이 쓸 거르개. 대상 직군을 비워 두었으면 거르지 않는다."""
    fields = target_fields.read_fields(conn)
    return Screener(conn, fields) if fields else None


def record_screened(
    conn: sqlite3.Connection, workflow_id: int, source_url: str, title: str, stage: str, reason: str
) -> None:
    """거른 공고를 남긴다. 같은 주소가 이미 있으면 그대로 둔다."""
    conn.execute(
        "INSERT OR IGNORE INTO screened_jobs (workflow_id, source_url, title, stage, reason)"
        " VALUES (?, ?, ?, ?, ?)",
        (workflow_id, source_url, title, stage, reason[:MAX_REASON_CHARS]),
    )


def is_screened(conn: sqlite3.Connection, workflow_id: int, source_url: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM screened_jobs WHERE workflow_id = ? AND source_url = ? LIMIT 1",
        (workflow_id, source_url),
    ).fetchone()
    return row is not None
