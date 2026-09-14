"""분류 테스트가 같이 쓰는 가짜 응답.

뽑는 칸은 조각 목록으로 답한다 (`app/classify/pieces.py`). 글자로 준 값은 줄마다 조각 하나로
바꾸고, 조각이 가리키는 줄은 `NOWHERE` 로 둔다.

응답은 직무마다 하나인 `postings` 와 공통 묶음 `common` 으로 온다 (`app/classify/schema.py`).
칸 이름으로만 값을 주면 공고 하나짜리 응답이 된다.
"""

from __future__ import annotations

import json
from typing import Any

from app.classify.schema import (
    COMMON,
    COMMON_FIELDS,
    EXTRACT_FIELDS,
    POSTING_FIELDS,
    POSTINGS,
    RESPONSE_FIELDS,
)

# 어느 줄도 아닌 번호. 옮기기가 "원문 어딘가에 그 글자가 있나" 로만 갈려서, 줄 번호를 모르는
# 테스트는 전처럼 원문에 있으면 남고 없으면 버려진다
NOWHERE = -1


def pieces(value: str, line: int = NOWHERE) -> list[dict[str, Any]]:
    """글자 값을 줄마다 조각 하나로 바꾼다."""
    return [{"line": line, "text": part} for part in value.splitlines() if part.strip()]


def _extract(value: Any) -> Any:
    return value if isinstance(value, list) else pieces(value)


def posting_body(**fields: Any) -> dict[str, Any]:
    """나눈 공고 하나. 뽑는 칸에 목록을 주면 그대로 쓰고, 글자를 주면 조각으로 바꾼다."""
    return {
        name: _extract(fields.get(name, "")) if name in EXTRACT_FIELDS else fields.get(name, "")
        for name in POSTING_FIELDS
    }


def common_body(**fields: Any) -> dict[str, Any]:
    """공통 묶음. 받는 방식은 `posting_body` 와 같다."""
    return {name: _extract(fields.get(name, "")) for name in COMMON_FIELDS}


def response_body(
    *,
    postings: list[dict[str, Any]] | None = None,
    common: dict[str, Any] | None = None,
    **fields: Any,
) -> dict[str, Any]:
    """응답 객체. `postings` 를 주지 않으면 칸 이름으로 받은 값이 공고 하나가 된다.

    제안 칸(`company_name_suggestion` 같은)은 공고가 아니라 응답 맨 위에 앉는다.
    """
    body: dict[str, Any] = {
        name: fields.get(name, "") for name in RESPONSE_FIELDS if name not in (COMMON, POSTINGS)
    }
    body[COMMON] = common if common is not None else common_body()
    body[POSTINGS] = postings if postings is not None else [posting_body(**fields)]
    return body


def response(**fields: Any) -> str:
    return json.dumps(response_body(**fields), ensure_ascii=False)
