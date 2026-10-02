"""미래내일 일경험 수집 설정. 값은 `app_settings` 표에 들어간다.

부트캠프 수집 설정(`app/bootcamp/settings.py`)과 같은 모양이다. 오공고 주소와 내부 API 키는 공고
전송과 같은 것을 쓴다. 여기 있는 것은 이 수집기만의 켜기·끄기와 시각이다.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

SCHEDULE_ENABLED = "work_experience_schedule_enabled"
RUN_TIME = "work_experience_run_time"
DELIVER_ENABLED = "work_experience_deliver_enabled"

KEYS: tuple[str, ...] = (SCHEDULE_ENABLED, RUN_TIME, DELIVER_ENABLED)

# 매일 한 번, 표시 시간대(`DISPLAY_TIMEZONE`, 기본 한국)의 이 시각에 수집한다. 크롤러 서버는 아침
# 몇 시간만 켜져 있어(08:30 매일 수집) 그 안에 둔다. 모집기간이 대개 1~2주라 하루 한 번이면 된다
DEFAULT_RUN_TIME = "09:30"
_RUN_TIME = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


class WorkExperienceSettingError(ValueError):
    """저장할 수 없는 값. 거절 사유를 화면이 그대로 옮긴다."""


@dataclass(frozen=True)
class WorkExperienceConfig:
    # 매일 수집을 켰는가. 꺼져 있어도 화면의 `지금 수집` 은 돈다
    schedule_enabled: bool = False
    # 매일 수집하는 시각 `HH:MM`
    run_time: str = DEFAULT_RUN_TIME
    # 수집이 끝나면 오공고로 보내는가. 꺼져 있어도 화면의 `지금 보내기` 는 보낸다
    deliver_enabled: bool = False

    def validate(self) -> None:
        if not _RUN_TIME.fullmatch(self.run_time):
            raise WorkExperienceSettingError(
                f"수집 시각은 00:00 부터 23:59 까지 HH:MM 으로 적는다: {self.run_time!r}"
            )

    @property
    def hour(self) -> int:
        return int(self.run_time[:2])

    @property
    def minute(self) -> int:
        return int(self.run_time[3:])


def read_config(conn: sqlite3.Connection) -> WorkExperienceConfig:
    stored = {
        str(row["key"]): str(row["value"])
        for row in conn.execute(
            f"SELECT key, value FROM app_settings WHERE key IN ({','.join('?' * len(KEYS))})",
            KEYS,
        )
    }
    run_time = stored.get(RUN_TIME, DEFAULT_RUN_TIME)
    return WorkExperienceConfig(
        schedule_enabled=stored.get(SCHEDULE_ENABLED, "0") == "1",
        run_time=run_time if _RUN_TIME.fullmatch(run_time) else DEFAULT_RUN_TIME,
        deliver_enabled=stored.get(DELIVER_ENABLED, "0") == "1",
    )


def write_config(conn: sqlite3.Connection, config: WorkExperienceConfig) -> WorkExperienceConfig:
    config.validate()
    values = {
        SCHEDULE_ENABLED: "1" if config.schedule_enabled else "0",
        RUN_TIME: config.run_time,
        DELIVER_ENABLED: "1" if config.deliver_enabled else "0",
    }
    for key, value in values.items():
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    return read_config(conn)
