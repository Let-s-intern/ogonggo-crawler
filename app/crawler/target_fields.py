"""수집·전송 대상 직군 (2026-10-08 결정, LC-3444). 값은 `app_settings` 한 행에 JSON 목록으로 든다.

오공고에는 마케팅·인사·기획·영업·개발 공고만 올린다. 처음에는 전송에서만 걸렀는데
(`app/deliver/spring.py`) 대상 밖 공고도 상세를 열고 본문을 분류해 비용이 그대로 나갔다. 그래서
수집에서 먼저 거르고(`app/crawler/screen.py`), 전송도 같은 목록을 읽는다.

이름은 오공고 직군의 한글 이름이다 (`app/job_roles.py`). 분류가 고르는 `job_field` 와 같은 글자다.

**저장된 값이 없으면 `DEFAULT_FIELDS` 다.** 목록을 비워 저장하면 거르지 않는다 — 수집도 전송도
전 직군이다. 읽기는 예외를 던지지 않는다. 깨진 값이면 기본값으로 떨어진다.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterable

from app import job_roles

logger = logging.getLogger(__name__)

KEY = "collect_target_fields"

# 마케팅(마케팅·광고, 상품기획·MD), 인사(HR·총무), 기획(기획·전략), 세일즈(영업),
# 개발(IT·개발, AI·데이터). 2026-10-06 전송 필터에서 정한 묶음 그대로다
DEFAULT_FIELDS: tuple[str, ...] = (
    "마케팅·광고",
    "상품기획·MD",
    "HR·총무",
    "기획·전략",
    "영업",
    "IT·개발",
    "AI·데이터",
)


class TargetFieldError(ValueError):
    """저장할 수 없는 값. 거절 사유를 화면이 그대로 옮긴다."""


def choices() -> tuple[str, ...]:
    """고를 수 있는 직군. 오공고 직군 순서다."""
    return job_roles.field_labels()


def read_fields(conn: sqlite3.Connection) -> tuple[str, ...]:
    """대상 직군. 비어 있으면 거르지 않는다는 뜻이다."""
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (KEY,)).fetchone()
    if row is None:
        return DEFAULT_FIELDS
    try:
        stored = json.loads(str(row["value"]))
    except json.JSONDecodeError:
        logger.warning("%s 에 저장된 값을 읽을 수 없다: %r. 기본값을 쓴다", KEY, row["value"])
        return DEFAULT_FIELDS
    if not isinstance(stored, list):
        logger.warning("%s 에 저장된 값이 목록이 아니다: %r. 기본값을 쓴다", KEY, row["value"])
        return DEFAULT_FIELDS
    known = set(choices())
    # 오공고 직군 이름이 바뀌어 목록 밖이 된 값은 버린다. 화면에서 다시 고르면 된다
    return tuple(name for name in choices() if name in stored and name in known)


def write_fields(conn: sqlite3.Connection, fields: Iterable[str]) -> tuple[str, ...]:
    """대상 직군을 저장한다. 목록 밖 이름이 하나라도 있으면 아무것도 저장하지 않는다."""
    chosen = {name.strip() for name in fields if name.strip()}
    unknown = sorted(chosen - set(choices()))
    if unknown:
        raise TargetFieldError(f"오공고 직군에 없는 이름이다: {', '.join(unknown)}")
    ordered = [name for name in choices() if name in chosen]
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')",
        (KEY, json.dumps(ordered, ensure_ascii=False)),
    )
    return tuple(ordered)
