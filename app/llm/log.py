"""모델 호출을 `llm_calls` 에 남긴다.

셀렉터 생성만 있을 때는 로그 줄 하나로 충분했다. 본문 분류는 공고마다 하나씩 붙어서, 로그가
사라지면 "이번 달에 얼마나 썼나" 에 답할 길이 없다 (`migrations/0013_llm_calls.sql`).

**기록 실패가 호출을 실패로 만들지 않는다.** 표가 없거나 DB 가 잠겨 있어도 분류는 계속
가야 한다 — 남기지 못한 것은 숫자 한 줄이고, 멈추면 사라지는 것은 그 공고의 분류다.
못 남긴 사실은 경고 로그로 남는다.

담지 않는 것: 프롬프트와 응답 본문. 비용을 세는 표에 사이트 본문이 한 벌 더 생길 이유가 없다.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from app.llm.base import Usage

logger = logging.getLogger(__name__)

# `llm_calls.feature` 에 들어가는 값. 무엇이 토큰을 썼는지 세는 잣대다
SELECTOR_GENERATE = "selector_generate"
SELECTOR_REPAIR = "selector_repair"
CLASSIFY = "classify"
# 본문이 이미지로만 올라온 공고의 글자를 수집할 때 읽는다 (`app/crawler/images.py`)
IMAGE_READ = "image_read"

FEATURES: tuple[str, ...] = (SELECTOR_GENERATE, SELECTOR_REPAIR, CLASSIFY, IMAGE_READ)


def record_call(
    conn: sqlite3.Connection,
    *,
    feature: str,
    usage: Usage,
    ok: bool = True,
    error: str = "",
    rules_version: int | None = None,
) -> int:
    """호출 하나를 남긴다. 실패해도 예외를 올리지 않고 0 을 돌려준다.

    실패한 호출도 남긴다. 토큰을 쓰고 실패하는 경우가 있어서 빼고 세면 합이 실제와 어긋난다.

    `rules_version` 은 분류 호출이 쓴 AI 규칙의 판이다. 판이 없는 호출(셀렉터 생성, 저장하지 않은
    규칙으로 돌린 시험)은 NULL 이다 (`migrations/0032_classify_rule_versions.sql`).

    제공자 이름은 `usage` 가 들고 온다. 여기서 설정을 다시 읽어 알아내면 호출과 기록 사이에
    설정이 바뀌었을 때 기록이 거짓이 된다 (`app/llm/base.py`).
    """
    try:
        cursor = conn.execute(
            """
            INSERT INTO llm_calls
                   (provider, model, feature, input_tokens, output_tokens, total_tokens,
                    latency_ms, ok, error, rules_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                usage.provider,
                usage.model,
                feature,
                usage.input_tokens,
                usage.output_tokens,
                usage.total_tokens,
                usage.latency_ms,
                int(ok),
                error[:500],
                rules_version,
            ),
        )
    except sqlite3.Error as exc:
        logger.warning("모델 호출 기록을 남기지 못했다 feature=%s: %s", feature, exc)
        return 0
    return int(cursor.lastrowid or 0)


def totals(conn: sqlite3.Connection, feature: str | None = None) -> dict[str, int]:
    """호출 수와 토큰 합. 기능을 주면 그 기능만 센다. 읽기 전용이다."""
    where, params = ("WHERE feature = ?", (feature,)) if feature else ("", ())
    row = conn.execute(
        f"""
        SELECT count(*) AS calls,
               coalesce(sum(input_tokens), 0)  AS input_tokens,
               coalesce(sum(output_tokens), 0) AS output_tokens,
               coalesce(sum(total_tokens), 0)  AS total_tokens,
               coalesce(sum(latency_ms), 0)    AS latency_ms,
               coalesce(sum(ok), 0)            AS ok
          FROM llm_calls {where}
        """,
        params,
    ).fetchone()
    return {name: int(row[name]) for name in row.keys()}


@dataclass(frozen=True)
class FeatureUsage:
    """기능 하나의 누적 사용량. 대시보드의 토큰 사용량 그래프가 이 값을 쓴다."""

    feature: str
    calls: int
    input_tokens: int
    output_tokens: int
    total_tokens: int


def by_feature(conn: sqlite3.Connection) -> list[FeatureUsage]:
    """기능별 누적 사용량. 호출이 한 번도 없는 기능도 0으로 나온다 — 그래프에서 아예
    빠지면 "그 기능이 있는지도" 알 수 없다. 순서는 `FEATURES` 순서로 고정한다."""
    result: list[FeatureUsage] = []
    for feature in FEATURES:
        t = totals(conn, feature)
        result.append(
            FeatureUsage(
                feature=feature,
                calls=t["calls"],
                input_tokens=t["input_tokens"],
                output_tokens=t["output_tokens"],
                total_tokens=t["total_tokens"],
            )
        )
    return result
