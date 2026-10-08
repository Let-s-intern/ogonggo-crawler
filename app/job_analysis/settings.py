"""매일 공고 분석 설정. 값은 `app_settings` 표에 들어간다.

미래내일 일경험 설정(`app/work_experience/settings.py`)과 같은 모양이다. 오공고 주소와 내부 API 키는
공고 전송과 같은 것을 쓴다. 여기 있는 것은 켜기·끄기와 시작·멈춤 시각이다.

## 시각 (2026-10-08 결정)

고용24 수집(오공고 서버, 04:00) → 크롤러 매일 수집(08:30 부터 150분) → 공고 분석 순서로 돈다. 그날
들어온 공고를 그날 분석하려면 매일 수집이 끝난 뒤에 시작해야 하고, 크롤러 서버가 12:00 에 꺼지기
전에 멈춰야 한다. 멈춤 시각이 되면 새 공고를 더 부르지 않고, 남은 공고는 다음 날 이어서 한다.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

SCHEDULE_ENABLED = "job_analysis_schedule_enabled"
START_TIME = "job_analysis_start_time"
STOP_TIME = "job_analysis_stop_time"

KEYS: tuple[str, ...] = (SCHEDULE_ENABLED, START_TIME, STOP_TIME)

DEFAULT_START_TIME = "11:10"
DEFAULT_STOP_TIME = "11:50"
_TIME = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


class JobAnalysisSettingError(ValueError):
    """저장할 수 없는 값. 거절 사유를 화면이 그대로 옮긴다."""


@dataclass(frozen=True)
class JobAnalysisConfig:
    # 매일 분석을 켰는가. 꺼져 있어도 화면의 `지금 분석` 은 돈다
    schedule_enabled: bool = False
    # 시작 시각 `HH:MM`
    start_time: str = DEFAULT_START_TIME
    # 멈춤 시각 `HH:MM`. 이 시각이 지나면 새 공고를 부르지 않는다
    stop_time: str = DEFAULT_STOP_TIME

    def validate(self) -> None:
        for label, value in (("시작", self.start_time), ("멈춤", self.stop_time)):
            if not _TIME.fullmatch(value):
                raise JobAnalysisSettingError(
                    f"{label} 시각은 00:00 부터 23:59 까지 HH:MM 으로 적는다: {value!r}"
                )
        if self.stop_time <= self.start_time:
            raise JobAnalysisSettingError("멈춤 시각은 시작 시각보다 늦어야 한다")

    @property
    def hour(self) -> int:
        return int(self.start_time[:2])

    @property
    def minute(self) -> int:
        return int(self.start_time[3:])


def read_config(conn: sqlite3.Connection) -> JobAnalysisConfig:
    stored = {
        str(row["key"]): str(row["value"])
        for row in conn.execute(
            f"SELECT key, value FROM app_settings WHERE key IN ({','.join('?' * len(KEYS))})",
            KEYS,
        )
    }
    start = stored.get(START_TIME, DEFAULT_START_TIME)
    stop = stored.get(STOP_TIME, DEFAULT_STOP_TIME)
    return JobAnalysisConfig(
        schedule_enabled=stored.get(SCHEDULE_ENABLED, "0") == "1",
        start_time=start if _TIME.fullmatch(start) else DEFAULT_START_TIME,
        stop_time=stop if _TIME.fullmatch(stop) else DEFAULT_STOP_TIME,
    )


def write_config(conn: sqlite3.Connection, config: JobAnalysisConfig) -> JobAnalysisConfig:
    config.validate()
    values = {
        SCHEDULE_ENABLED: "1" if config.schedule_enabled else "0",
        START_TIME: config.start_time,
        STOP_TIME: config.stop_time,
    }
    for key, value in values.items():
        conn.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    return read_config(conn)
