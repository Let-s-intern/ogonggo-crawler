"""사이트에서 못 읽은 회사 이름·모집 시작·모집 마감을 AI 에게 따로 묻는다 (2026-09-17 결정).

셀렉터는 사이트마다 정해진 자리에서 글자를 읽는다. 회사 이름과 모집 기간은 사이트마다 적는 자리가
달라서(`서류 제출 마감 기한은 9/20(일) 23:59 입니다` 처럼 문장 속에도 있다) 셀렉터로는 자주 빈다.
공고를 읽는 AI 가 뜻으로 찾는다.

본문 분류와 같은 방법이다 — 줄마다 번호를 붙여 보내고, AI 는 몇 번 줄의 어느 부분인지만 답하고,
저장하는 글자는 원문에서 잘라 온다 (`app/classify/pieces.py`). AI 가 없는 말을 지어낼 수 없다.

**본문 분류와 따로 묻는다.** 처음에는 분류 응답의 칸으로 넣었는데, 칸이 스무 개 넘는 긴 응답
안에서 DeepSeek 는 이 세 칸을 거의 짚지 않았다(APR 13건 중 마감 2건). 세 칸만 묻는 짧은 호출은
토큰이 적고 놓치지 않는다. 사이트에서 다 읽었으면 부르지 않는다.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, Field

from app.classify.classifier import ClassifyError, _Asker, build_client, chosen
from app.classify.pieces import number_lines, render, resolve
from app.classify.schema import FALLBACK_FIELDS, ClassifySchemaError, LinePiece
from app.config import Settings, get_settings
from app.llm.base import Usage

KIND = "회사·모집 기간"
MAX_CHARS = 12000

_INSTRUCTION = (
    "너는 채용공고에서 회사 이름과 모집 기간이 적힌 자리를 짚는 도구다. 글자를 지어내지 않고 "
    "줄 번호와 그 줄의 글자만 답한다."
)

_PROMPT = """아래는 채용공고다. 줄마다 앞에 [번호] 가 붙어 있고 [0] 이 제목이다.
다음 칸마다 그 값이 적힌 곳을 {{"line": 줄 번호, "text": 그 줄에서 해당하는 부분}} 하나로 답한다.
원문에 없으면 빈 목록([])이다. text 는 줄에 적힌 글자 그대로 옮기고, 줄 앞의 [번호] 는 넣지 않는다.
소제목이 없어도 문장 안에 있으면 짚는다.

{fields}

[공고]
{body}
"""

_FIELD_GUIDES: dict[str, str] = {
    "company_name": (
        "- company_name: 이 공고를 낸 회사 이름만. `에이피알 본사` 이면 `에이피알`, "
        "`(주)카카오` 이면 `(주)카카오`. 그룹 채용 사이트의 계열사 공고는 그 계열사 이름"
    ),
    "recruitment_start_at": (
        "- recruitment_start_at: 모집·접수 시작 날짜와 시각만. `접수기간 9/1(월) ~ 9/20(일)` 이면 "
        "`9/1(월)`"
    ),
    "recruitment_end_at": (
        "- recruitment_end_at: 모집·접수·서류 마감 날짜와 시각만. `서류 제출 마감 기한은 "
        "9/20(일) 23:59 입니다` 이면 `9/20(일) 23:59`. `~ 9/20(일)` 이면 `9/20(일)`. "
        "상시 채용·채용 시 마감처럼 날짜가 없으면 빈 목록"
    ),
}


class Basics(BaseModel):
    company_name: list[LinePiece] = Field(default_factory=list)
    recruitment_start_at: list[LinePiece] = Field(default_factory=list)
    recruitment_end_at: list[LinePiece] = Field(default_factory=list)


assert tuple(Basics.model_fields) == FALLBACK_FIELDS


def build_prompt(body: str, title: str, needed: Sequence[str]) -> str:
    lines = number_lines(title, body[:MAX_CHARS])
    return _PROMPT.format(
        fields="\n".join(_FIELD_GUIDES[name] for name in needed), body=render(lines)
    )


def _parse(text: str) -> dict[str, tuple[int, str] | None]:
    """칸마다 첫 조각. 모르는 칸은 읽지 않는다 — 세 칸 말고는 쓸 곳이 없다."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClassifySchemaError("unparsable", f"JSON 이 아니다: {exc}") from exc
    if not isinstance(data, Mapping):
        raise ClassifySchemaError("unparsable", "응답이 객체가 아니다")
    found: dict[str, tuple[int, str] | None] = {}
    for name in FALLBACK_FIELDS:
        pieces = data.get(name) or []
        if isinstance(pieces, Mapping):
            pieces = [pieces]
        first = next(
            (
                item
                for item in pieces
                if isinstance(item, Mapping) and str(item.get("text") or "").strip()
            ),
            None,
        )
        try:
            found[name] = (
                None if first is None else (int(first.get("line", -1)), str(first["text"]))
            )
        except (TypeError, ValueError):
            found[name] = (-1, str(first["text"])) if first is not None else None
    return found


async def find_basics(
    body: str,
    title: str,
    needed: Sequence[str],
    *,
    settings: Settings | None = None,
    client: Any | None = None,
    on_call: Callable[[Usage], None] | None = None,
) -> dict[str, str]:
    """`needed` 칸마다 원문에서 옮겨 온 글자. 못 찾은 칸은 빈 문자열이다."""
    wanted = [name for name in FALLBACK_FIELDS if name in needed]
    if not wanted or not body.strip():
        return {}
    resolved = settings or get_settings()
    provider, model = chosen(resolved)
    asker = _Asker(client or build_client(resolved), model, provider, on_call)
    found, _, _ = await asker.ask(
        build_prompt(body, title, wanted),
        schema=Basics,
        instruction=_INSTRUCTION,
        kind=KIND,
        parse=_parse,
    )
    lines = number_lines(title, body)
    values: dict[str, str] = {}
    for name in wanted:
        piece = found.get(name)
        values[name] = resolve([piece], lines).value if piece else ""
    return values


__all__ = ["ClassifyError", "find_basics"]
