"""분류가 원문에서 짚어 온 날짜 글자를 일시로 읽는다 (2026-09-17 결정).

사이트에서 읽은 날짜는 그 사이트에 맞춘 규칙이 읽는다(`date_parse`). 규칙은 틀린 형식을 만나면
실패하고 그 공고의 정규화가 멈춘다 — 사이트 하나의 형식이 바뀐 것을 알아야 하기 때문이다. 분류가
짚은 글자는 사이트마다 제각각이라(`9/20(일) 23: 59`, `2026년 9월 20일`) 같은 규칙을 걸면 공고가
통째로 빠진다. 그래서 여기서는 글자 안에서 날짜를 찾기만 하고, 못 찾으면 빈 값이다.

연도가 없으면 수집한 해로 본다. 그렇게 본 날짜가 수집일보다 반년 넘게 앞이면 다음 해다 — 12월에
수집한 공고의 `1/10` 마감은 다음 해 1월이다.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

OUTPUT_FORMAT = "%Y-%m-%d %H:%M:%S"

_WITH_YEAR = re.compile(
    r"(?P<year>(?:19|20)\d{2})\s*[.\-/년]\s*(?P<month>\d{1,2})\s*[.\-/월]\s*(?P<day>\d{1,2})"
)
_WITHOUT_YEAR = re.compile(r"(?<![\d.])(?P<month>\d{1,2})\s*(?:[./]|월)\s*(?P<day>\d{1,2})(?![\d])")
_TIME = re.compile(r"(?P<hour>\d{1,2})\s*:\s*(?P<minute>\d{2})")
_HALF_YEAR = timedelta(days=183)


def read(text: str, collected_on: date) -> str | None:
    """글자에서 처음 나오는 날짜(와 그 뒤 시각)를 일시로. 못 읽으면 None."""
    found = _WITH_YEAR.search(text)
    if found is not None:
        year = int(found["year"])
    else:
        found = _WITHOUT_YEAR.search(text)
        if found is None:
            return None
        year = collected_on.year
    try:
        day = date(year, int(found["month"]), int(found["day"]))
    except ValueError:
        return None
    if found.re is _WITHOUT_YEAR and day < collected_on - _HALF_YEAR:
        try:
            day = day.replace(year=year + 1)
        except ValueError:
            return None
    hour = minute = 0
    clock = _TIME.search(text, found.end())
    if clock is not None and int(clock["hour"]) < 24 and int(clock["minute"]) < 60:
        hour, minute = int(clock["hour"]), int(clock["minute"])
    return datetime(day.year, day.month, day.day, hour, minute).strftime(OUTPUT_FORMAT)
