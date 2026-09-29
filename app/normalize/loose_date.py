"""분류가 원문에서 짚어 온 날짜 글자를 일시로 읽는다 (2026-09-17 결정).

사이트에서 읽은 날짜는 그 사이트에 맞춘 규칙이 읽는다(`date_parse`). 규칙은 틀린 형식을 만나면
실패하고 그 공고의 정규화가 멈춘다 — 사이트 하나의 형식이 바뀐 것을 알아야 하기 때문이다. 분류가
짚은 글자는 사이트마다 제각각이라(`9/20(일) 23: 59`, `2026년 9월 20일`) 같은 규칙을 걸면 공고가
통째로 빠진다. 그래서 여기서는 글자 안에서 날짜를 찾기만 하고, 못 찾으면 빈 값이다.

연도가 없으면 수집한 해로 본다. 그렇게 본 날짜가 수집일보다 반년 넘게 앞이면 다음 해다 — 12월에
수집한 공고의 `1/10` 마감은 다음 해 1월이다. `26.10.31` 처럼 두 자리 연도도 읽는다.

마감일에 날짜가 없고 남은 날 수만 있으면(`D-32`, `D-day`, `오늘 마감`) 수집한 날에서 센다
(2026-09-29 결정). 그 숫자는 수집한 날 화면에 보인 값이라 기준이 수집일이다. 날짜가 함께 적혀
있으면 날짜가 먼저다. 시작일은 세지 않는다 — 적힌 날짜가 없으면 빈 값이고, 그때 정규화가 수집한
날을 시작일로 둔다 (`app/normalize/engine.py` 의 `fill_fallbacks`).

사이트 셀렉터가 마감 칸에서 `D-32` 를 읽는 경우도 같은 셈을 쓴다 (`settle_counted`). 규칙의
`date_parse` 는 `D-32` 를 읽지 못해 그 공고의 정규화가 통째로 멈추므로, 규칙을 태우기 전에 날짜로
바꾼다. 날짜가 함께 적혀 있으면(`2026.10.31 (D-32)`) 남은 날 글자만 뗀다.

마감일에 시각이 없으면 그날 23:59:59 다 — 시작일은 00:00 이다. 사이트 규칙과 같은 기준이다
(`app/normalize/engine.py` 의 `_DAY_BOUNDARY`). `18시`, `오후 6시` 도 시각으로 읽는다.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta

OUTPUT_FORMAT = "%Y-%m-%d %H:%M:%S"

_WITH_YEAR = re.compile(
    r"(?P<year>(?:19|20)\d{2})\s*[.\-/년]\s*(?P<month>\d{1,2})\s*[.\-/월]\s*(?P<day>\d{1,2})"
)
_SHORT_YEAR = re.compile(
    r"(?<![\d.])(?P<year>\d{2})\s*[.\-/]\s*(?P<month>\d{1,2})\s*[.\-/]\s*(?P<day>\d{1,2})(?!\d)"
)
_WITHOUT_YEAR = re.compile(r"(?<![\d.])(?P<month>\d{1,2})\s*(?:[./]|월)\s*(?P<day>\d{1,2})(?![\d])")
_D_DAY = re.compile(r"(?<![A-Za-z])D\s*-\s*(?:(?P<days>\d{1,3})(?!\d)|day)", re.IGNORECASE)
_TODAY = re.compile(r"(?P<word>오늘|금일|내일)\s*마감")
_TIME = re.compile(
    r"(?P<half>오전|오후|AM|PM)?\s*(?P<hour>\d{1,2})\s*"
    r"(?::\s*(?P<minute>\d{2})|시(?:\s*(?P<minutes>\d{1,2})\s*분)?)",
    re.IGNORECASE,
)
# 날짜 옆에 붙은 남은 날 글자. 괄호째 뗀다
_COUNTED_WORDS = re.compile(
    r"\(?\s*(?:(?<![A-Za-z])D\s*-\s*(?:\d{1,3}(?!\d)|day)|(?:오늘|금일|내일)\s*마감)\s*\)?",
    re.IGNORECASE,
)
_HALF_YEAR = timedelta(days=183)
_END_OF_DAY = time(23, 59, 59)


def read(text: str, collected_on: date, *, end: bool = False) -> str | None:
    """글자에서 처음 나오는 날짜(와 그 뒤 시각)를 일시로. 못 읽으면 None.

    `end` 가 참이면 마감일이다. 시각이 없을 때 그날 끝(23:59:59)으로 둔다.
    """
    found = _date(text, collected_on)
    if found is None:
        # 남은 날 수는 마감에만 쓴다. 시작일 자리의 `D-32` 는 시작일이 아니다
        counted = _counted(text, collected_on) if end else None
        if counted is None:
            return None
        return datetime.combine(counted, _END_OF_DAY).strftime(OUTPUT_FORMAT)
    day, after = found
    clock = _clock(text, after)
    if clock is None:
        clock = _END_OF_DAY if end else time()
    return datetime.combine(day, clock).strftime(OUTPUT_FORMAT)


def settle_counted(text: str, collected_on: date) -> str:
    """사이트에서 읽은 마감 글자의 `D-32`·`오늘 마감` 을 규칙이 읽을 수 있게 바꾼다.

    남은 날 글자가 없으면 그대로다. 날짜가 함께 있으면 남은 날 글자만 떼고, 날짜가 없으면 수집한
    날에서 센 날짜(`2026-10-19`)로 바꾼다. 그 뒤는 사이트 규칙이 읽는다.
    """
    if _D_DAY.search(text) is None and _TODAY.search(text) is None:
        return text
    if _date(text, collected_on) is not None:
        return _COUNTED_WORDS.sub(" ", text).strip()
    counted = _counted(text, collected_on)
    return text if counted is None else counted.isoformat()


def _date(text: str, collected_on: date) -> tuple[date, int] | None:
    """적힌 날짜와 그 날짜가 끝나는 자리. 날짜가 없으면 None."""
    for pattern in (_WITH_YEAR, _SHORT_YEAR):
        found = pattern.search(text)
        if found is None:
            continue
        year = int(found["year"])
        year = year + 2000 if year < 100 else year
        try:
            return date(year, int(found["month"]), int(found["day"])), found.end()
        except ValueError:
            continue
    found = _WITHOUT_YEAR.search(text)
    if found is None:
        return None
    try:
        day = date(collected_on.year, int(found["month"]), int(found["day"]))
        if day < collected_on - _HALF_YEAR:
            day = day.replace(year=day.year + 1)
    except ValueError:
        return None
    return day, found.end()


def _counted(text: str, collected_on: date) -> date | None:
    """`D-32`·`D-day`·`오늘 마감` 처럼 수집한 날에서 센 날짜. 없으면 None."""
    found = _D_DAY.search(text)
    if found is not None:
        return collected_on + timedelta(days=int(found["days"] or 0))
    word = _TODAY.search(text)
    if word is not None:
        return collected_on + timedelta(days=1 if word["word"] == "내일" else 0)
    return None


def _clock(text: str, start: int) -> time | None:
    """날짜 뒤에 적힌 시각. `23:59`, `18시`, `오후 6시 30분` 을 읽는다. 없으면 None."""
    found = _TIME.search(text, start)
    if found is None:
        return None
    hour = int(found["hour"])
    minute = int(found["minute"] or found["minutes"] or 0)
    if (found["half"] or "").upper() in ("오후", "PM") and hour < 12:
        hour += 12
    if hour >= 24 or minute >= 60:
        return None
    return time(hour, minute)
