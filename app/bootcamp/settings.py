"""부트캠프 수집 설정. 값은 `app_settings` 표에 들어간다.

공고 전송 설정(`app/deliver/settings.py`)과 같은 자리다. 오공고 주소와 내부 API 키는 공고
전송과 같은 것을 쓴다. 여기 있는 것은 부트캠프만의 켜기·끄기와 주기다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

SCHEDULE_ENABLED = "bootcamp_schedule_enabled"
INTERVAL_HOURS = "bootcamp_interval_hours"
DELIVER_ENABLED = "bootcamp_deliver_enabled"

KEYS: tuple[str, ...] = (SCHEDULE_ENABLED, INTERVAL_HOURS, DELIVER_ENABLED)

# 새싹 모집 중 과정은 몇 건이고 모집 기간이 몇 주다. 하루 두 번이면 새 과정을 늦지 않게 잡는다
DEFAULT_INTERVAL_HOURS = 12


class BootcampSettingError(ValueError):
    """저장할 수 없는 값. 거절 사유를 화면이 그대로 옮긴다."""


@dataclass(frozen=True)
class BootcampConfig:
    # 주기 수집을 켰는가. 꺼져 있어도 화면의 `지금 수집` 은 돈다
    schedule_enabled: bool = False
    interval_hours: int = DEFAULT_INTERVAL_HOURS
    # 수집이 끝나면 오공고로 보내는가. 꺼져 있어도 화면의 `지금 보내기` 는 보낸다
    deliver_enabled: bool = False

    def validate(self) -> None:
        if not 1 <= self.interval_hours <= 24 * 7:
            raise BootcampSettingError(
                f"수집 주기는 1시간부터 168시간까지다: {self.interval_hours}"
            )


def read_config(conn: sqlite3.Connection) -> BootcampConfig:
    stored = {
        str(row["key"]): str(row["value"])
        for row in conn.execute(
            f"SELECT key, value FROM app_settings WHERE key IN ({','.join('?' * len(KEYS))})",
            KEYS,
        )
    }
    try:
        hours = int(stored.get(INTERVAL_HOURS, DEFAULT_INTERVAL_HOURS))
    except ValueError:
        hours = DEFAULT_INTERVAL_HOURS
    return BootcampConfig(
        schedule_enabled=stored.get(SCHEDULE_ENABLED, "0") == "1",
        interval_hours=hours if hours >= 1 else DEFAULT_INTERVAL_HOURS,
        deliver_enabled=stored.get(DELIVER_ENABLED, "0") == "1",
    )


def write_config(conn: sqlite3.Connection, config: BootcampConfig) -> BootcampConfig:
    config.validate()
    values = {
        SCHEDULE_ENABLED: "1" if config.schedule_enabled else "0",
        INTERVAL_HOURS: str(config.interval_hours),
        DELIVER_ENABLED: "1" if config.deliver_enabled else "0",
    }
    for key, value in values.items():
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    return read_config(conn)
