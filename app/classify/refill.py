"""공고 목록의 `AI로 다시 채우기` 가 공고마다 어디까지 왔는지 (2026-09-30 결정, LC-3394).

확인 필요 공고를 한꺼번에 다시 채우면 수십 건이 한 건씩 돈다. 운영자는 목록에서 행마다
시작 전(`AI 대기`)인지, 도는 중(`AI 채우는 중`)인지, 끝났는지(`AI 다시 채움`·`AI 채우기 실패`)를
본다. 패널에서 한 건만 다시 채울 때도 같은 표시가 붙는다.

한꺼번에 다시 채우기는 분류 자리(`ClassifyRun.claim`)를 잡고 돈다. 그동안 다른 분류는 물러난다 —
같은 공고에 두 번 돈을 쓰지 않는다. 한 번에 도는 건수는 분류 상한(`MAX_LIMIT`)을 넘지 않는다.

상태는 한 프로세스 안의 메모리에 둔다. 분류 진행 상황과 같은 이유다 (`app/classify/batch.py`).
프로세스가 다시 뜨면 표시는 사라지고, 분류 결과는 DB 에 남는다.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterable

from app.classify.batch import (
    JOB_DONE,
    JOB_FAILED,
    JOB_RUNNING,
    ClassifyProgress,
    ClassifyRun,
    bounded,
    classify_ids,
)
from app.normalize.backfill import ConnectFactory

logger = logging.getLogger(__name__)

JOB_WAITING = "waiting"

# 행에 붙일 표시 (낱말, 색). 목록의 다른 표시와 같은 칩이다
LABELS: dict[str, tuple[str, str]] = {
    JOB_WAITING: ("AI 대기", "idle"),
    JOB_RUNNING: ("AI 채우는 중", "warn"),
    JOB_DONE: ("AI 다시 채움", "ok"),
    JOB_FAILED: ("AI 채우기 실패", "bad"),
}

# 아직 끝나지 않은 상태. 목록의 표시가 이 동안만 스스로 다시 묻는다
PENDING = frozenset({JOB_WAITING, JOB_RUNNING})


class RefillBoard:
    """수집 건 번호마다 AI 다시 채우기 상태."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[int, str] = {}
        self._thread: threading.Thread | None = None

    def mark(self, raw_job_id: int, state: str) -> None:
        with self._lock:
            self._states[raw_job_id] = state

    def state(self, raw_job_id: int) -> str | None:
        with self._lock:
            return self._states.get(raw_job_id)

    def states(self, raw_job_ids: Iterable[int]) -> dict[int, str]:
        with self._lock:
            return {i: self._states[i] for i in raw_job_ids if i in self._states}

    def start(self, connect: ConnectFactory, run: ClassifyRun, raw_job_ids: list[int]) -> int:
        """백그라운드로 다시 채운다. 몇 건을 걸었는지 돌려준다.

        분류가 돌고 있으면 `ClassifyRunningError`. 상한을 넘는 건은 걸지 않는다.
        """
        picked = raw_job_ids[: bounded(len(raw_job_ids))] if raw_job_ids else []
        claimed = run.claim()
        with self._lock:
            for raw_job_id in picked:
                self._states[raw_job_id] = JOB_WAITING
        self._thread = threading.Thread(
            target=self._work, args=(connect, run, claimed, picked), name="refill", daemon=True
        )
        self._thread.start()
        return len(picked)

    def wait(self, timeout: float | None = None) -> bool:
        """작업이 끝날 때까지 기다린다. 끝났으면 True. 테스트가 쓴다."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _work(
        self,
        connect: ConnectFactory,
        run: ClassifyRun,
        claimed: ClassifyProgress,
        raw_job_ids: list[int],
    ) -> None:
        conn = connect()
        try:
            asyncio.run(classify_ids(conn, raw_job_ids, claimed, on_job=self.mark))
        except Exception:
            logger.exception("AI 다시 채우기가 중단됐다")
        finally:
            conn.close()
            # 모델 설정이 틀려 한 건도 못 돌았거나 중단됐으면 남은 것은 실패다. 대기로 두면
            # 목록이 끝나지 않는 일을 영원히 기다린다
            with self._lock:
                for raw_job_id in raw_job_ids:
                    if self._states.get(raw_job_id) in PENDING:
                        self._states[raw_job_id] = JOB_FAILED
            run.release()


_board = RefillBoard()


def get_refill_board() -> RefillBoard:
    """앱이 쓰는 표시판 하나. 테스트는 이 의존성을 갈아끼운다."""
    return _board
