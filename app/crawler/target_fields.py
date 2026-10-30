"""수집·전송 대상 직군 (2026-10-08 결정, LC-3444). 값은 `app_settings` 한 행에 JSON 목록으로 든다.

오공고에는 정한 직군의 공고만 올린다. 처음에는 전송에서만 걸렀는데(`app/deliver/spring.py`) 대상 밖
공고도 상세를 열고 본문을 분류해 비용이 그대로 나갔다. 그래서 수집에서 먼저 거르고
(`app/crawler/screen.py`), 전송도 같은 목록을 읽는다.

공고 목록의 직군 칸에서 직군마다 켜고 끈다 (2026-10-19, `app/api/ui_target_fields.py`). 이름은
오공고 직군의 한글 이름이다 (`app/job_roles.py`). 분류가 고르는 `job_field` 와 같은 글자다.

**저장된 값이 없으면 `DEFAULT_FIELDS` 다.** 모두 켜면 거르지 않는다 — 수집도 전송도 전 직군이다.
모두 끄는 것은 받지 않는다. 읽기는 예외를 던지지 않는다. 깨진 값이면 기본값으로 떨어진다.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable

from app import job_roles

logger = logging.getLogger(__name__)

KEY = "collect_target_fields"

# 마케팅·인사·기획·영업·개발. 2026-10-06 전송 필터에서 정한 묶음이고, 오공고가 2026-10-12 직군을
# 합친 뒤의 이름이다
DEFAULT_FIELDS: tuple[str, ...] = (
    "IT·개발",
    "기획·전략",
    "마케팅·광고",
    "영업",
    "HR·총무",
)


# 인턴~3년차. 최소 경력이 이보다 긴 공고는 수집에서 거르고(`app/crawler/screen.py`) 보내지도
# 않는다(`app/deliver/spring.py` 의 `TARGET_MAX_YEARS`). 오공고 고용24 수집과 같은 값이다
MAX_CAREER_YEARS = 3
# "N년 이상" 처럼 위가 열린 경력 조건에서 대상으로 보는 N 의 상한. 수집(`app/crawler/screen.py`)과
# 전송·공고 목록의 `1-3년`(`app/deliver/spring.py` 의 `TARGET_YEARS_SQL`)이 같이 쓴다
MAX_OPEN_MIN_YEARS = 2


class TargetFieldError(ValueError):
    """저장할 수 없는 값. 거절 사유를 화면이 그대로 옮긴다."""


def choices() -> tuple[str, ...]:
    """고를 수 있는 직군. 오공고 직군 순서다."""
    return job_roles.field_labels()


def read_fields(conn: sqlite3.Connection) -> tuple[str, ...]:
    """켜 둔 직군. 오공고 직군 순서다."""
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (KEY,)).fetchone()
    if row is None:
        return _ordered(DEFAULT_FIELDS)
    try:
        stored = json.loads(str(row["value"]))
    except json.JSONDecodeError:
        logger.warning("%s 에 저장된 값을 읽을 수 없다: %r. 기본값을 쓴다", KEY, row["value"])
        return _ordered(DEFAULT_FIELDS)
    if not isinstance(stored, list):
        logger.warning("%s 에 저장된 값이 목록이 아니다: %r. 기본값을 쓴다", KEY, row["value"])
        return _ordered(DEFAULT_FIELDS)
    # 오공고 직군 이름이 바뀌어 목록 밖이 된 값은 버린다. 화면에서 다시 켜면 된다
    fields = _ordered(stored)
    return fields or _ordered(DEFAULT_FIELDS)


def filtering_fields(conn: sqlite3.Connection) -> tuple[str, ...]:
    """거를 때 쓰는 직군. 모두 켜 두었으면 비어 있다 — 거르지 않는다."""
    fields = read_fields(conn)
    return () if set(fields) >= set(choices()) else fields


def write_fields(conn: sqlite3.Connection, fields: Iterable[str]) -> tuple[str, ...]:
    """켜 둘 직군을 저장한다. 목록 밖 이름이 있거나 하나도 없으면 아무것도 저장하지 않는다."""
    chosen = {name.strip() for name in fields if name.strip()}
    unknown = sorted(chosen - set(choices()))
    if unknown:
        raise TargetFieldError(f"오공고 직군에 없는 이름이다: {', '.join(unknown)}")
    if not chosen:
        raise TargetFieldError("직군을 하나는 켜 둬야 한다")
    ordered = _ordered(chosen)
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')",
        (KEY, json.dumps(ordered, ensure_ascii=False)),
    )
    return ordered


def set_field(conn: sqlite3.Connection, field: str, on: bool) -> tuple[str, ...]:
    """직군 하나를 켜거나 끈다. 공고 목록의 스위치 하나가 이것 하나다."""
    current = set(read_fields(conn))
    return write_fields(conn, current | {field} if on else current - {field})


def _ordered(names: Iterable[object]) -> tuple[str, ...]:
    wanted = {str(name) for name in names}
    return tuple(name for name in choices() if name in wanted)
