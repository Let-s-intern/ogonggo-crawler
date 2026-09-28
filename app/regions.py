"""근무 지역. 분류가 공고마다 시·도 하나와 그 안의 시·군·구 하나를 고른다 (LC-3385, 2026-09-28).

오공고가 근무 지역을 글자로 받다가 enum 두 칸(`region`, `subRegion`)으로 바꿨다. 크롤러도 그 enum
이름(`SEOUL`, `SEOUL_GANGNAM_GU`)을 그대로 저장하고 보내며, 화면에만 한글 이름을 보인다 — 다른
판정 칸과 같은 방법이다 (`app/classify/schema.py`). 목록은 오공고 enum 을 옮긴 씨앗 파일에 있다
(`seeds/regions-ogonggo-20260928.json`). 오공고 enum 이 바뀌면 그 파일을 다시 뽑는다.

## 하나만 고른다

2026-09-21 부터는 근무지가 여러 곳이면 모두 골라 `서울, 경기` 로 이었다. 오공고 칸은 하나라 이제는
첫 번째 근무지 하나다. `전국`(`NATIONWIDE`)과 `해외`(`OVERSEAS`)는 시·군·구가 없다.

## 시·군·구는 시·도 안에서만

같은 이름의 구(중구·동구·강서구)가 여러 시·도에 있어 이름이 시·도로 시작한다. 오공고는 시·도와 맞지
않는 시·군·구를 400 으로 거절하므로, 맞지 않으면 시·군·구만 버린다 (`fits`).

표로 두지 않는다. 직무·산업 분류와 달리 운영 중에 화면에서 바꿀 목록이 아니다.
"""

from __future__ import annotations

import json
import pathlib
from functools import cache
from typing import Any

SEED_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "seeds" / "regions-ogonggo-20260928.json"
)


@cache
def _seed() -> list[dict[str, Any]]:
    return list(json.loads(SEED_PATH.read_text(encoding="utf-8"))["regions"])


@cache
def labels() -> dict[str, str]:
    """시·도 이름과 화면 이름. 순서는 오공고 enum 순서다."""
    return {str(region["name"]): str(region["label"]) for region in _seed()}


@cache
def sub_labels() -> dict[str, str]:
    """시·군·구 이름과 화면 이름. 화면 이름에 시·도를 붙인다 — `중구` 만으로는 어디인지 모른다."""
    return {
        str(sub["name"]): f"{region['label']} {sub['label']}"
        for region in _seed()
        for sub in region["subRegions"]
    }


@cache
def _parents() -> dict[str, str]:
    return {
        str(sub["name"]): str(region["name"]) for region in _seed() for sub in region["subRegions"]
    }


def names() -> tuple[str, ...]:
    """고를 수 있는 시·도."""
    return tuple(labels())


def sub_names() -> tuple[str, ...]:
    """고를 수 있는 시·군·구. 시·도 순서대로다."""
    return tuple(sub_labels())


@cache
def subs_of() -> dict[str, tuple[str, ...]]:
    """시·도마다 그 안의 시·군·구. 시·군·구가 없는 시·도(`전국`·`세종`·`해외`)는 빈 묶음이다."""
    return {
        str(region["name"]): tuple(str(sub["name"]) for sub in region["subRegions"])
        for region in _seed()
    }


def fits(region: str, sub_region: str) -> bool:
    """시·군·구가 그 시·도 안에 있는가. 시·군·구가 비었으면 맞다."""
    return not sub_region or _parents().get(sub_region) == region
