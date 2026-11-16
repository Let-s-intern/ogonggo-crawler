"""미래내일 일경험 설정. 값은 `app_settings` 표에 들어간다.

오공고 주소와 내부 API 키는 공고 전송과 같은 것을 쓴다. 여기 있는 것은 이 기능만의 전송 켜기·끄기다.
매일 수집 켜기·시각(`work_experience_schedule_enabled`·`work_experience_run_time`)은 포털 수집을
걷으며 뺐다 (2026-11-16, LC-3605). 표에 남은 옛 값은 읽지 않는다.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

DELIVER_ENABLED = "work_experience_deliver_enabled"


@dataclass(frozen=True)
class WorkExperienceConfig:
    # CSV 를 정리하고 나면 오공고로 보내는가. 꺼져 있어도 화면의 `지금 보내기` 는 보낸다
    deliver_enabled: bool = False


def read_config(conn: sqlite3.Connection) -> WorkExperienceConfig:
    row = conn.execute(
        "SELECT value FROM app_settings WHERE key = ?", (DELIVER_ENABLED,)
    ).fetchone()
    return WorkExperienceConfig(deliver_enabled=row is not None and str(row["value"]) == "1")


def write_config(conn: sqlite3.Connection, config: WorkExperienceConfig) -> WorkExperienceConfig:
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (DELIVER_ENABLED, "1" if config.deliver_enabled else "0"),
    )
    return read_config(conn)
