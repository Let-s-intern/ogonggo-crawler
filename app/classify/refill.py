"""공고 목록의 `AI로 다시 채우기` 가 공고마다 어디까지 왔는지 (2026-09-30 결정, LC-3394).

확인 필요 공고를 한꺼번에 다시 채우면 수십 건이 한 건씩 돈다. 운영자는 목록에서 행마다
시작 전(`AI 대기`)인지, 도는 중(`AI 채우는 중`)인지, 끝났는지(`AI 다시 채움`·`AI 채우기 실패`)를
본다. 패널에서 한 건만 다시 채울 때도 같은 표시가 붙는다.

한꺼번에 다시 채우기는 분류 자리(`ClassifyRun.claim`)를 잡고 돈다. 그동안 다른 분류는 물러난다 —
같은 공고에 두 번 돈을 쓰지 않는다.

## 고른 것을 끝까지 돈다 (2026-10-15)

전에는 한 번에 분류 상한(`MAX_LIMIT`, 200건)까지만 걸고 "남은 것은 끝난 뒤 다시 누른다" 고 했다.
언제 끝나는지 알 수 없었고, 다시 누르면 조건에 걸린 공고를 번호순으로 다시 앞에서부터 200건
잡았다 — `마감일 없음` 처럼 다시 채워도 그대로 남는 공고에 돈이 두 번 들었다. 이제 고른 것을
`CHUNK` 건씩 끝까지 돌고, 묶음 사이에서 멈추기를 본다. 건수는 화면이 걸기 전에 묻는다.

## 무엇이 바뀌었는지 남긴다

운영자가 다시 채우는 까닭은 대개 마감일이다. 시작할 때 공고마다 마감일을 적어 두고, 끝나면
지금 값과 견줘 새로 찾은 것과 바뀐 것을 센다. 그중 이미 오공고로 보낸 공고는 다시 채워도 오공고에
저절로 가지 않는다(`app/deliver/spring.py` — 분류 뒤 자동 전송은 새 공고만 등록한다). 그래서 결과
요약에 따로 세고, 화면이 그 공고만 골라 오공고 값을 바꾸는 단추를 단다.

상태는 한 프로세스 안의 메모리에 둔다. 분류 진행 상황과 같은 이유다 (`app/classify/batch.py`).
프로세스가 다시 뜨면 표시는 사라지고, 분류 결과는 DB 에 남는다.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.classify.batch import (
    JOB_DONE,
    JOB_FAILED,
    JOB_RUNNING,
    ClassifyProgress,
    ClassifyRun,
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

# 한 묶음에 도는 건수. 멈추기는 묶음 사이에서만 먹는다 — 작을수록 빨리 멈추고, 묶음마다 모델 설정과
# 규칙을 다시 읽는 비용이 붙는다
CHUNK = 20


@dataclass(frozen=True)
class DeadlineChange:
    """다시 채우기 전후로 마감일이 달라진 공고 하나."""

    normalized_id: int
    raw_job_id: int
    title: str
    before: str
    after: str
    sent: bool


@dataclass
class RefillRun:
    """한꺼번에 다시 채우기 한 번. 화면이 진행과 결과를 그린다."""

    total: int = 0
    done: int = 0
    failed: int = 0
    running: bool = False
    stopping: bool = False
    stopped: bool = False
    started_at: str = ""
    finished_at: str = ""
    # 끝난 뒤에만 찬다. 비었던 마감일을 찾은 것과, 있던 마감일이 다른 날로 바뀐 것
    found: list[DeadlineChange] = field(default_factory=list)
    changed: list[DeadlineChange] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def left(self) -> int:
        return max(0, self.total - self.done - self.failed)

    def snapshot(self) -> RefillRun:
        return RefillRun(
            **{
                **vars(self),
                "found": list(self.found),
                "changed": list(self.changed),
                "errors": list(self.errors),
            }
        )

    def sent_changes(self) -> list[DeadlineChange]:
        """마감일이 달라졌는데 이미 오공고로 보낸 공고. 오공고 값을 바꿔야 반영된다."""
        return [change for change in (*self.found, *self.changed) if change.sent]


class RefillBoard:
    """수집 건 번호마다 AI 다시 채우기 상태."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[int, str] = {}
        self._thread: threading.Thread | None = None
        self._run: RefillRun | None = None

    def mark(self, raw_job_id: int, state: str) -> None:
        with self._lock:
            self._states[raw_job_id] = state

    def _count(self, raw_job_id: int, state: str) -> None:
        """한꺼번에 다시 채우기의 공고 하나가 끝났거나 시작했다. 행 표시와 건수를 같이 적는다."""
        with self._lock:
            self._states[raw_job_id] = state
            if self._run is not None and state == JOB_DONE:
                self._run.done += 1
            elif self._run is not None and state == JOB_FAILED:
                self._run.failed += 1

    def run(self) -> RefillRun | None:
        """지금 도는, 또는 마지막으로 끝난 한꺼번에 다시 채우기. 없으면 None."""
        with self._lock:
            return self._run.snapshot() if self._run is not None else None

    def stop(self) -> bool:
        """지금 묶음을 끝내고 멈춘다. 멈출 것이 있었으면 True."""
        with self._lock:
            if self._run is None or not self._run.running:
                return False
            self._run.stopping = True
            return True

    def dismiss(self) -> None:
        """끝난 결과를 화면에서 치운다. 도는 중이면 치우지 않는다."""
        with self._lock:
            if self._run is not None and not self._run.running:
                self._run = None

    def state(self, raw_job_id: int) -> str | None:
        with self._lock:
            return self._states.get(raw_job_id)

    def states(self, raw_job_ids: Iterable[int]) -> dict[int, str]:
        with self._lock:
            return {i: self._states[i] for i in raw_job_ids if i in self._states}

    def start(self, connect: ConnectFactory, run: ClassifyRun, raw_job_ids: list[int]) -> int:
        """백그라운드로 고른 것을 끝까지 다시 채운다. 몇 건을 걸었는지 돌려준다.

        분류가 돌고 있으면 `ClassifyRunningError`.
        """
        picked = list(dict.fromkeys(raw_job_ids))
        claimed = run.claim()
        conn = connect()
        try:
            before = _deadlines(conn, picked)
        finally:
            conn.close()
        with self._lock:
            for raw_job_id in picked:
                self._states[raw_job_id] = JOB_WAITING
            self._run = RefillRun(total=len(picked), running=True, started_at=_now())
        self._thread = threading.Thread(
            target=self._work,
            args=(connect, run, claimed, picked, before),
            name="refill",
            daemon=True,
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
        before: dict[int, dict[int, str]],
    ) -> None:
        conn = connect()
        try:
            for start in range(0, len(raw_job_ids), CHUNK):
                with self._lock:
                    stopping = self._run is not None and self._run.stopping
                if stopping:
                    break
                chunk = raw_job_ids[start : start + CHUNK]
                progress = asyncio.run(classify_ids(conn, chunk, claimed, on_job=self._count))
                if progress.errors:
                    with self._lock:
                        if self._run is not None and len(self._run.errors) < 5:
                            self._run.errors.extend(progress.errors[: 5 - len(self._run.errors)])
        except Exception:
            logger.exception("AI 다시 채우기가 중단됐다")
        finally:
            found: list[DeadlineChange] = []
            changed: list[DeadlineChange] = []
            try:
                found, changed = _changes(conn, before)
            except sqlite3.Error:
                logger.exception("AI 다시 채우기: 마감일 변화를 세지 못했다")
            conn.close()
            # 모델 설정이 틀려 한 건도 못 돌았거나 중단됐으면 남은 것은 실패다. 대기로 두면
            # 목록이 끝나지 않는 일을 영원히 기다린다. 멈춘 것은 손대지 않았으니 표시를 지운다
            with self._lock:
                stopped = self._run is not None and self._run.stopping
                for raw_job_id in raw_job_ids:
                    if self._states.get(raw_job_id) in PENDING:
                        if stopped and self._states[raw_job_id] == JOB_WAITING:
                            del self._states[raw_job_id]
                        else:
                            self._states[raw_job_id] = JOB_FAILED
                            if self._run is not None:
                                self._run.failed += 1
                if self._run is not None:
                    self._run.running = False
                    self._run.stopped = stopped
                    self._run.finished_at = _now()
                    self._run.found = found
                    self._run.changed = changed
            run.release()
            logger.info(
                "AI 다시 채우기 끝: 마감일 새로 찾음 %s건, 바뀜 %s건%s",
                len(found),
                len(changed),
                " (멈춤)" if stopped else "",
            )


_board = RefillBoard()


def get_refill_board() -> RefillBoard:
    """앱이 쓰는 표시판 하나. 테스트는 이 의존성을 갈아끼운다."""
    return _board


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _chunks(ids: Sequence[int], size: int = 500) -> Iterable[tuple[list[int], str]]:
    for start in range(0, len(ids), size):
        part = list(ids[start : start + size])
        yield part, ",".join("?" for _ in part)


def _day(value: object) -> str:
    """마감일을 날짜로. 시각 차이(`23:59` 와 `23:59:00`)는 같은 마감으로 본다."""
    return str(value or "").strip()[:10]


def _deadlines(conn: sqlite3.Connection, raw_job_ids: Sequence[int]) -> dict[int, dict[int, str]]:
    """공고마다 나뉜 공고 번호별 마감일. 다시 채우기 전후를 견주는 데 쓴다."""
    found: dict[int, dict[int, str]] = {raw_job_id: {} for raw_job_id in raw_job_ids}
    for part, marks in _chunks(raw_job_ids):
        for row in conn.execute(
            "SELECT raw_job_id, part, recruitment_end_at FROM normalized_jobs"
            f" WHERE raw_job_id IN ({marks})",
            part,
        ):
            found[int(row["raw_job_id"])][int(row["part"])] = _day(row["recruitment_end_at"])
    return found


def _changes(
    conn: sqlite3.Connection, before: dict[int, dict[int, str]]
) -> tuple[list[DeadlineChange], list[DeadlineChange]]:
    """지금 마감일을 시작할 때와 견준다. 새로 찾은 것과 다른 날로 바뀐 것."""
    found: list[DeadlineChange] = []
    changed: list[DeadlineChange] = []
    for part, marks in _chunks(list(before)):
        rows = conn.execute(
            f"""
            SELECT n.id, n.raw_job_id, n.part, n.title, n.recruitment_end_at,
                   EXISTS (SELECT 1 FROM spring_deliveries d
                            WHERE d.source_url = n.source_url AND d.status = 'sent') AS sent
              FROM normalized_jobs n
             WHERE n.raw_job_id IN ({marks})
             ORDER BY n.raw_job_id, n.part
            """,
            part,
        ).fetchall()
        for row in rows:
            after = _day(row["recruitment_end_at"])
            earlier = before.get(int(row["raw_job_id"]), {}).get(int(row["part"]), "")
            if after == earlier or not after:
                continue
            change = DeadlineChange(
                normalized_id=int(row["id"]),
                raw_job_id=int(row["raw_job_id"]),
                title=str(row["title"] or ""),
                before=earlier,
                after=after,
                sent=bool(row["sent"]),
            )
            (changed if earlier else found).append(change)
    return found, changed
