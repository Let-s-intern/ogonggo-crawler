"""근무지 목록. 분류가 공고마다 이 안에서 고른다 (2026-09-21 결정).

근무지는 원문 글자를 그대로 옮기던 칸이었다. 같은 곳이 `울산`, `울산광역시 동구`, `본사(울산)` 로
제각각 쌓여 소비 측이 그 칸으로 거를 수 없었다. 이제 큰 지역 열아홉 개 중에서 고른다 — `전국`,
서울부터 제주까지 열일곱 시·도, `해외` 다.

목록은 직행(zighang.com) 채용공고 지역 필터에서 왔다 (`seeds/regions-zighang-20260921.json`).
그 필터의 `전체` 는 고르는 값이 아니라 필터의 선택지라 뺀다. 구·시·군 목록은 고르는 값이 아니고,
씨앗 파일에 남겨 둔다.

## 여러 개를 고른다

근무지가 여러 곳인 공고가 흔하다 — HD현대 신입 공채가 울산·분당·대산·영암이다. 그래서 여러 개를
고르고, `서울, 경기` 처럼 쉼표로 이어 한 칸에 저장한다. 순서는 목록 순서다 — 같은 공고가 분류할
때마다 다른 순서로 저장되면 바뀐 것이 없는데 바뀐 것으로 보인다.

## `전국` 은 하나로 둔다

`전국 현장`·`전국 각지` 처럼 곳을 특정하지 않는 공고가 있다. 직행 필터에는 없는 값이라 여기서
더한다. 전국이 나머지를 다 포함하므로 `전국` 을 고르면 다른 지역은 붙이지 않는다 — `본사(양재동) /
전국 현장` 은 `서울, 전국` 이 아니라 `전국` 이다 (2026-09-21 결정).

표로 두지 않는다. 직무·산업 분류와 달리 운영 중에 바뀔 일이 없는 목록이다.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterable
from functools import cache

SEED_PATH = (
    pathlib.Path(__file__).resolve().parent.parent / "seeds" / "regions-zighang-20260921.json"
)

# 여러 근무지를 한 칸에 이을 때 쓰는 글자
SEPARATOR = ", "
# 곳을 특정하지 않는 공고. 씨앗 파일(직행 필터)에는 없어 코드가 맨 앞에 더한다
NATIONWIDE = "전국"


@cache
def names() -> tuple[str, ...]:
    """고를 수 있는 큰 지역. `전국` 이 맨 앞이고 나머지는 씨앗 파일의 순서 그대로다."""
    data = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    return (NATIONWIDE, *(str(region["name"]) for region in data["regions"]))


def split(value: str) -> list[str]:
    """저장된 한 칸을 지역 이름들로. 빈 조각은 버린다."""
    return [part.strip() for part in value.split(",") if part.strip()]


def join(values: Iterable[str]) -> str:
    """지역 이름들을 한 칸으로. 목록 밖 이름은 버리고, 겹친 것은 하나로, 순서는 목록 순서다.

    `전국` 이 있으면 `전국` 하나다. 나머지를 다 포함한다.
    """
    wanted = set(values)
    if NATIONWIDE in wanted:
        return NATIONWIDE
    return SEPARATOR.join(name for name in names() if name in wanted)
