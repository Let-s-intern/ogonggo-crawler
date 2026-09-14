"""목록에서 읽은 마감일로 그 공고의 상세를 열지 말지 정한다.

**수집은 날짜 형식을 새로 알지 않는다.** 어떤 표기를 날짜로 읽을지는 정규화 규칙
(`app/normalize/rules.py` 의 `date_parse`)이 이미 알고 있고, 여기서는 그 규칙을 그대로 태워
나온 값만 본다. 같은 사이트의 같은 값이 화면과 수집에서 다르게 읽히는 일을 만들지 않는다.

## 읽지 못한 날짜는 진행 중이다

판정은 한쪽으로만 기운다. 지났다고 확실히 읽은 것만 마감이고, 나머지는 전부 진행 중이다.

| 목록에서 읽은 값 | 판정 |
|---|---|
| 오늘보다 이전 날짜 | 마감 |
| 오늘 | 진행 중 |
| 오늘보다 뒤 날짜 | 진행 중 |
| 빈 값 (상시채용) | 진행 중 |
| 규칙이 날짜로 읽지 못한 값 | 진행 중 |

날짜 형식이 바뀐 사이트를 조용히 전부 버리는 것이 이 기능의 유일한 위험이다. 못 읽은 것을
지난 것으로 취급하면 그 사이트의 공고가 한 건도 들어오지 않는데, 실행은 실패로 남지도 않는다.

## 비교는 표시 시간대의 오늘로 한다

마감 시각이 있어도 날짜로 비교한다. UTC 의 오늘과 비교하면 한국 시각 오전 아홉 시간 동안 하루
일찍 마감된 것으로 보인다. 검수 화면의 `진행중`/`마감 지남` 도 같은 기준을 쓴다
(`app/api/review_filter.py`).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime

from app.api.ui import display_zone
from app.normalize.engine import NormalizeError, normalize_value
from app.normalize.rules import Rule

DEADLINE = "recruitment_end_at"


def today() -> date:
    """진행 여부를 가르는 오늘. 표시 시간대의 날짜다."""
    return datetime.now(display_zone()).date()


def is_closed(value: str, rules: Sequence[Rule], on: date | None = None) -> bool:
    """목록에서 읽은 마감일이 오늘보다 이전인가. 읽지 못한 값은 진행 중이라 False 다.

    `on` 은 시험이 오늘을 고정하려고 주는 값이다. 주지 않으면 표시 시간대의 오늘이다.
    """
    parsed = _as_date(value, rules)
    if parsed is None:
        return False
    return parsed < (on or today())


def _as_date(value: str, rules: Sequence[Rule]) -> date | None:
    """정규화 규칙을 태운 뒤 날짜로 읽는다. 어느 단계에서든 읽지 못하면 None 이다."""
    if not value.strip():
        return None

    try:
        normalized = normalize_value(DEADLINE, value, rules)
    except NormalizeError:
        # 규칙이 어느 형식으로도 읽지 못한 값이다. 사이트가 표기를 바꿨을 수 있으므로
        # 버리지 않고 진행 중으로 둔다
        return None
    if not normalized:
        # 규칙이 값을 비웠다. `상시채용` 을 mapping 으로 비우는 경로가 이것이다
        return None

    try:
        # 0033 부터 마감 시각까지 저장한다. 비교는 날짜로 한다 — 오늘 마감인 공고는 오늘 하루
        # 진행 중이다
        return date.fromisoformat(normalized.strip()[:10])
    except ValueError:
        # 규칙의 `output_format` 이 날짜 형식이 아니다. 마감으로 단정할 근거가 없다
        return None
