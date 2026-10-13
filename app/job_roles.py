"""오공고 직군·직무 enum (LC-3385, 2026-09-29).

오공고가 직군·직무를 글자로 받다가 enum(`IT_DEVELOPMENT`, `IT_BACKEND`)으로 바꿨다. 한글
이름이 그대로 오면 400 이다. 크롤러 직무 분류표(`job_taxonomy`)는 오공고 enum 과 한글 이름이
하나하나 같아, 표와 공고에는 한글 이름을 그대로 두고 보낼 때만 enum 이름으로 바꾼다. 목록은 오공고
enum 을 옮긴 씨앗 파일에 있다 (`seeds/job-roles-ogonggo-20260929.json`). 오공고 enum 이 바뀌면 그
파일을 다시 뽑는다.

직무는 직군마다 `기타` 같은 이름이 겹칠 수 있어 직군과 함께 찾는다. 오공고는 직군에 속하지 않는
직무를 400 으로 거절하므로, 직군을 모르면 직무도 보내지 않는다.

2026-10-12 오공고가 직군 여섯 개를 다섯 직군으로 합치고 그 직무를 합쳐진 직군 아래로 옮겼다
(`MERGED_FIELDS`). 크롤러 분류표와 씨앗 파일은 그대로 두고, 보낼 때 직군만 합쳐진 직군으로 바꾼다.
직무 enum 이름은 오공고도 바꾸지 않았다.
"""

from __future__ import annotations

import json
import pathlib
from functools import cache
from typing import Any

SEED_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "seeds" / "job-roles-ogonggo-20260929.json"
)


@cache
def _seed() -> list[dict[str, Any]]:
    return list(json.loads(SEED_PATH.read_text(encoding="utf-8"))["jobFields"])


@cache
def _fields() -> dict[str, str]:
    return {str(field["label"]): str(field["name"]) for field in _seed()}


@cache
def _roles() -> dict[tuple[str, str], str]:
    return {
        (str(field["label"]), str(role["label"])): str(role["name"])
        for field in _seed()
        for role in field["roles"]
    }


# 오공고에서 없어진 직군 → 합쳐진 직군 (2026-10-12). 오공고 `JobField` 주석과 같다
MERGED_FIELDS: dict[str, str] = {
    "AI_DATA": "IT_DEVELOPMENT",
    "MERCHANDISING": "PLANNING_STRATEGY",
    "DESIGN": "PLANNING_STRATEGY",
    "ACCOUNTING_TAX_FINANCE": "HR_GENERAL_AFFAIRS",
    "CUSTOMER_SERVICE_TM": "HR_GENERAL_AFFAIRS",
    "MEDIA_ENTERTAINMENT": "MARKETING_ADVERTISING",
}


def enum_names(field: str | None, role: str | None) -> tuple[str | None, str | None]:
    """한글 직군·직무를 오공고 enum 이름으로. 목록 밖이면 None 이다 — 둘 다 선택 칸이다.

    합쳐진 직군의 직무는 직군을 합쳐진 직군으로 바꿔 보낸다. 오공고에서 그 직무의 직군이 바뀌었다.
    """
    seed_field = _fields().get(field) if field else None
    if seed_field is None:
        return None, None
    field_name = MERGED_FIELDS.get(seed_field, seed_field)
    if not role:
        return field_name, None
    return field_name, _roles().get((str(field), role))
