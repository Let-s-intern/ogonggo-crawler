"""회사 이름·모집 시작·모집 마감을 AI 에게 따로 묻는다 (2026-09-17 결정, 2026-10-08 바뀜).

회사 이름은 사이트에서 못 읽었을 때만 묻는다. **모집 시작·마감은 사이트에서 읽었어도 늘 묻는다**
(2026-10-08 결정). 사이트가 따로 읽은 값을 공고 끝에 줄로 붙여 함께 보여 주고, AI 가 원문과 견줘
고른 값을 무조건 쓴다 — 사이트 칸이 다른 공고의 날짜를 읽었거나 틀린 형식이어도 그대로 나가던 것을
막는다. 사이트 값도 번호 붙은 줄이라 AI 가 그 줄을 고르면 사이트 값이 된다.

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
import re
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
        "`(주)카카오` 이면 `(주)카카오`. 그룹 채용 사이트의 계열사 공고는 그 계열사 이름. "
        "회사 안의 팀·부서·본부·센터·조직 이름은 넣지 않는다 — `토스 결제플랫폼팀` 이면 `토스`, "
        "`카카오 AI본부 추천팀` 이면 `카카오`. 회사 이름 없이 팀 이름만 적혀 있으면 빈 목록"
    ),
    "recruitment_start_at": (
        "- recruitment_start_at: 모집·접수 시작 날짜와 시각만. `접수기간 9/1(월) ~ 9/20(일)` 이면 "
        "`9/1(월)`"
    ),
    "recruitment_end_at": (
        "- recruitment_end_at: 모집·접수·서류 마감 날짜와 시각만. `서류 제출 마감 기한은 "
        "9/20(일) 23:59 입니다` 이면 `9/20(일) 23:59`. `~ 9/20(일)` 이면 `9/20(일)`. "
        "날짜 없이 `D-32`·`D-day`·`오늘 마감` 처럼 남은 날만 적혀 있으면 그 글자(`D-32`). "
        "상시 채용·채용 시 마감처럼 날짜도 남은 날도 없으면 빈 목록"
    ),
}

# 날짜 칸을 물을 때 붙이는 안내. 사이트가 따로 읽은 값이 공고 끝 줄에 있다
_SITE_GUIDE = (
    "- 공고 끝의 `[사이트 칸]` 줄은 사이트의 정해진 자리에서 따로 읽은 모집 기간이다. 맞는지 "
    "원문과 "
    "견줘 판단한다. 본문에 모집 기간이 있고 사이트 칸과 다르면 본문을 짚는다. 본문에 없으면 "
    "사이트 칸 "
    "줄을 짚는다. 사이트 칸이 모집 기간이 아닌 것(작성일·수정일·근무 기간)을 읽었으면 짚지 않는다"
)
_SITE_LABELS: dict[str, str] = {
    "recruitment_start_at": "모집 시작",
    "recruitment_end_at": "모집 마감",
}


class Basics(BaseModel):
    company_name: list[LinePiece] = Field(default_factory=list)
    recruitment_start_at: list[LinePiece] = Field(default_factory=list)
    recruitment_end_at: list[LinePiece] = Field(default_factory=list)


assert tuple(Basics.model_fields) == FALLBACK_FIELDS


def numbered(body: str, title: str, site_values: Mapping[str, str]) -> list[str]:
    """번호를 붙일 줄. 제목과 본문 뒤에 사이트가 따로 읽은 모집 기간을 한 줄씩 붙인다."""
    extra = [
        f"[사이트 칸] {_SITE_LABELS[name]}: {value.strip()}"
        for name, value in site_values.items()
        if name in _SITE_LABELS and value.strip()
    ]
    return [*number_lines(title, body[:MAX_CHARS]), *extra]


def build_prompt(lines: Sequence[str], needed: Sequence[str], *, site: bool = False) -> str:
    guides = [_FIELD_GUIDES[name] for name in needed]
    if site:
        guides.append(_SITE_GUIDE)
    return _PROMPT.format(fields="\n".join(guides), body=render(lines))


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
    site_values: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """`needed` 칸마다 원문에서 옮겨 온 글자. 못 찾은 칸은 빈 문자열이다.

    `site_values` 는 사이트가 따로 읽은 모집 시작·마감이다. 공고 끝에 줄로 붙어 AI 가 원문과 견준다.
    """
    wanted = [name for name in FALLBACK_FIELDS if name in needed]
    if not wanted or not body.strip():
        return {}
    site = {name: value for name, value in (site_values or {}).items() if name in wanted}
    lines = numbered(body, title, site)
    resolved = settings or get_settings()
    provider, model = chosen(resolved)
    asker = _Asker(client or build_client(resolved), model, provider, on_call)
    found, _, _ = await asker.ask(
        build_prompt(lines, wanted, site=any(value.strip() for value in site.values())),
        schema=Basics,
        instruction=_INSTRUCTION,
        kind=KIND,
        parse=_parse,
    )
    values: dict[str, str] = {}
    for name in wanted:
        piece = found.get(name)
        values[name] = _without_site_label(resolve([piece], lines).value) if piece else ""
    return values


_SITE_LABEL = re.compile(r"^\s*(\[사이트 칸\]\s*)?(모집 시작|모집 마감)\s*:\s*")


def _without_site_label(text: str) -> str:
    """사이트 칸 줄을 골랐으면 줄 앞의 이름표(`[사이트 칸] 모집 마감:`)를 뗀다. 값만 남긴다."""
    return _SITE_LABEL.sub("", text)


__all__ = ["ClassifyError", "find_basics"]
