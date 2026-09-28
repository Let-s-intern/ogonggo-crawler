"""매일 한 바퀴 수집의 설정과 순서. 스케줄러 잡은 `app/scheduler.py` 가 건다.

크롤 워크플로우는 저마다의 주기로 돌지 않는다. 매일 정해진 시각(기본 08:30, 표시 시간대 기준)에
한 바퀴가 시작되고, active 인 사이트를 id 순서로 **한 곳씩** 돈다. 시작 간격은 `spread_minutes`
를 사이트 수로 나눈 값이라 전체가 그 시간에 걸쳐 흩어진다 (2026-09-28 결정).

주기 대신 이렇게 하는 이유는 서버가 하루에 3~4시간만 켜져 있기 때문이다. 주기 잡은 프로세스가
뜬 시점부터 세므로, 하루 주기(1440분)는 서버가 꺼지기 전에 한 번도 깨어나지 못한다.

오늘 이미 스케줄 실행이 있었던 사이트는 한 바퀴에서 뺀다. 그래서 한 바퀴 도중에 서버가 다시
떠도 처음부터 다시 돌지 않고 남은 곳만 돈다.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

from app.config import get_settings

START_TIME = "daily_crawl_time"
SPREAD_MINUTES = "daily_crawl_spread_minutes"

KEYS: tuple[str, ...] = (START_TIME, SPREAD_MINUTES)

DEFAULT_START_TIME = "08:30"
# 2~3시간에 걸쳐 돌린다. 서버가 켜져 있는 3~4시간 안에 끝나야 한다
DEFAULT_SPREAD_MINUTES = 150
# 하루를 넘기면 다음 바퀴와 겹친다
MAX_SPREAD_MINUTES = 12 * 60

_START_TIME = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


class DailySettingError(ValueError):
    """저장할 수 없는 값. 거절 사유를 화면이 그대로 옮긴다."""


@dataclass(frozen=True)
class DailyConfig:
    # 한 바퀴를 시작하는 시각 `HH:MM`. 표시 시간대 기준이다
    start_time: str = DEFAULT_START_TIME
    # 한 바퀴를 몇 분에 걸쳐 흩어 돌리는가
    spread_minutes: int = DEFAULT_SPREAD_MINUTES

    def validate(self) -> None:
        if not _START_TIME.fullmatch(self.start_time):
            raise DailySettingError(
                f"시작 시각은 00:00 부터 23:59 까지 HH:MM 으로 적는다: {self.start_time!r}"
            )
        if not 0 <= self.spread_minutes <= MAX_SPREAD_MINUTES:
            raise DailySettingError(
                f"걸쳐 도는 시간은 0 부터 {MAX_SPREAD_MINUTES} 분 사이다: {self.spread_minutes}"
            )

    @property
    def hour(self) -> int:
        return int(self.start_time[:2])

    @property
    def minute(self) -> int:
        return int(self.start_time[3:])


def zone() -> tzinfo:
    return ZoneInfo(get_settings().display_timezone)


def read_config(conn: sqlite3.Connection) -> DailyConfig:
    stored = {
        str(row["key"]): str(row["value"])
        for row in conn.execute(
            f"SELECT key, value FROM app_settings WHERE key IN ({','.join('?' * len(KEYS))})",
            KEYS,
        )
    }
    config = DailyConfig(
        start_time=stored.get(START_TIME, DEFAULT_START_TIME),
        spread_minutes=_int_or(stored.get(SPREAD_MINUTES), DEFAULT_SPREAD_MINUTES),
    )
    try:
        config.validate()
    except DailySettingError:
        # 손으로 넣은 값이 깨져 있어도 수집은 멈추지 않는다
        return DailyConfig()
    return config


def write_config(conn: sqlite3.Connection, config: DailyConfig) -> DailyConfig:
    config.validate()
    values = {START_TIME: config.start_time, SPREAD_MINUTES: str(config.spread_minutes)}
    for key, value in values.items():
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')",
            (key, value),
        )
    return config


def _int_or(raw: str | None, default: int) -> int:
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default


def round_start(config: DailyConfig, now: datetime) -> datetime:
    """`now` 가 속한 날의 한 바퀴 시작 시각. 표시 시간대의 날짜로 가른다."""
    local = now.astimezone(zone())
    return datetime.combine(local.date(), time(config.hour, config.minute), tzinfo=zone())


def round_window(config: DailyConfig, now: datetime) -> tuple[datetime, datetime]:
    """오늘 한 바퀴의 (시작, 끝). 끝이 지나면 남은 곳이 있어도 오늘은 더 돌지 않는다."""
    start = round_start(config, now)
    return start, start + timedelta(minutes=config.spread_minutes)


def active_workflows(conn: sqlite3.Connection) -> list[int]:
    """한 바퀴에 드는 크롤 워크플로우. id 순서가 곧 도는 순서다.

    직접 넣은 공고의 워크플로우(`kind = 'manual'`)는 돌 목록이 없어 빼 둔다.
    """
    rows = conn.execute(
        "SELECT id FROM workflows WHERE kind = 'crawl' AND status = 'active' ORDER BY id"
    ).fetchall()
    return [int(row["id"]) for row in rows]


def remaining_today(conn: sqlite3.Connection, config: DailyConfig, now: datetime) -> list[int]:
    """오늘 한 바퀴에서 아직 돌지 않은 곳. 오늘 시작 시각 뒤에 스케줄 실행이 있었으면 뺀다."""
    since = _utc_text(round_start(config, now))
    done = {
        int(row["workflow_id"])
        for row in conn.execute(
            "SELECT DISTINCT workflow_id FROM crawl_runs"
            " WHERE trigger = 'schedule' AND workflow_id IS NOT NULL AND started_at >= ?",
            (since,),
        )
    }
    return [workflow_id for workflow_id in active_workflows(conn) if workflow_id not in done]


def slots(workflow_ids: list[int], begin: datetime, end: datetime) -> dict[int, datetime]:
    """각 곳의 시작 예정 시각. `begin` 부터 `end` 까지 고르게 나눈다.

    `end` 가 이미 지났으면 간격이 0 이 되어 한 곳이 끝나는 대로 다음 곳이 돈다.
    """
    if not workflow_ids:
        return {}
    gap = max(end - begin, timedelta(0)) / len(workflow_ids)
    return {workflow_id: begin + gap * index for index, workflow_id in enumerate(workflow_ids)}


def _utc_text(value: datetime) -> str:
    """`crawl_runs.started_at` 과 같은 꼴(UTC, 시간대 표시 없음)."""
    return value.astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")
