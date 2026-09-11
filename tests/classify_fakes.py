"""분류 테스트가 같이 쓰는 가짜 응답.

뽑는 칸은 조각 목록으로 답한다 (`app/classify/pieces.py`). 글자로 준 값은 줄마다 조각 하나로
바꾸고, 조각이 가리키는 줄은 `NOWHERE` 로 둔다.
"""

from __future__ import annotations

import json
from typing import Any

from app.classify.schema import EXTRACT_FIELDS, RESPONSE_FIELDS

# 어느 줄도 아닌 번호. 옮기기가 "원문 어딘가에 그 글자가 있나" 로만 갈려서, 줄 번호를 모르는
# 테스트는 전처럼 원문에 있으면 남고 없으면 버려진다
NOWHERE = -1


def pieces(value: str, line: int = NOWHERE) -> list[dict[str, Any]]:
    """글자 값을 줄마다 조각 하나로 바꾼다."""
    return [{"line": line, "text": part} for part in value.splitlines() if part.strip()]


def response_body(**fields: Any) -> dict[str, Any]:
    """응답 객체. 뽑는 칸에 목록을 주면 그대로 쓰고, 글자를 주면 조각으로 바꾼다."""
    body: dict[str, Any] = {}
    for name in RESPONSE_FIELDS:
        value = fields.get(name, "")
        if name in EXTRACT_FIELDS and not isinstance(value, list):
            value = pieces(value)
        body[name] = value
    return body


def response(**fields: Any) -> str:
    return json.dumps(response_body(**fields), ensure_ascii=False)
