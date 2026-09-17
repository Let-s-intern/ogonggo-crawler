"""이 프로세스가 방금 낸 로그 최근 줄을 메모리에 들고 있는다.

이 버퍼를 보여 주던 대시보드는 2026-09-17 에 없앴다(LC-3344). 지금은 읽는 화면이 없다. 파일도,
두 번째 프로세스도 두지 않는다 — 이 프로세스가 지금 내는 로그를 화면에서 바로 보는 용도라
재시작하면 비는 것이 맞고, `docker compose logs` 를 대체하려는 것도 아니다.

`app` 로거 하나에만 붙는다(`app/main.py`). uvicorn 자체 로거나 그 아래 라이브러리 로그까지
담으면 100줄이 접속·헬스체크 잡음으로 금방 밀려나 정작 크롤링·분류 같은 운영 이벤트가
안 보인다.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from threading import Lock

CAPACITY = 100


@dataclass(frozen=True)
class LogLine:
    time: str
    level: str
    logger: str
    message: str


class RingBufferHandler(logging.Handler):
    """최근 `capacity` 줄만 남기는 로그 핸들러.

    요청을 처리하는 스레드와 APScheduler 워커 스레드가 동시에 로그를 남길 수 있어 잠근다.
    """

    def __init__(self, capacity: int = CAPACITY) -> None:
        super().__init__()
        self._lines: deque[LogLine] = deque(maxlen=capacity)
        self._lock = Lock()

    def emit(self, record: logging.LogRecord) -> None:
        # 서버가 도는 시스템 시각 그대로다(운영자 시간대 변환 없음). 방금 난 것을 훑어보는
        # 자리라 상대적인 순서만 맞으면 되고, `display_zone()` 을 쓰려면 이 모듈이 화면
        # 계층(`app.api.ui`)에 기대는 역방향 의존이 생긴다
        line = LogLine(
            time=datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
            level=record.levelname,
            logger=record.name,
            message=record.getMessage(),
        )
        with self._lock:
            self._lines.append(line)

    def tail(self) -> list[LogLine]:
        """지금까지 쌓인 줄 전부(최대 `capacity`개). 오래된 것이 먼저다."""
        with self._lock:
            return list(self._lines)

    def clear(self) -> None:
        """버퍼를 비운다. 테스트가 이전 테스트가 남긴 줄과 섞이지 않게 쓴다."""
        with self._lock:
            self._lines.clear()


# 프로세스당 하나. `app/main.py` 가 `app` 로거에 붙이고, 대시보드 조각 라우트가 읽는다
handler = RingBufferHandler()
