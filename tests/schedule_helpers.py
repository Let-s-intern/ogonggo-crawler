"""스케줄러 테스트가 같이 쓰는 확인 함수."""

from __future__ import annotations

import sqlite3

from app.crawler import daily
from app.scheduler import DAILY_JOB_ID, WorkflowScheduler


def in_round(conn: sqlite3.Connection, scheduler: WorkflowScheduler) -> list[int]:
    """매일 한 바퀴에 드는 워크플로우. 매일 잡이 걸려 있어야 한 바퀴가 돈다."""
    assert scheduler.scheduler.get_job(DAILY_JOB_ID) is not None
    return daily.active_workflows(conn)
