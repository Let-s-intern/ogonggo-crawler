"""수동 재정규화.

운영자가 명시적으로 실행할 때만 도는 동작이다. 규칙을 저장해도 기존 `normalized_jobs` 는
그대로고, 버튼을 눌렀을 때만 `raw_jobs` 를 다시 읽어 갱신한다 (2026-08-21 결정).

규칙 저장에 재처리를 묶지 않는 이유는 하나다. 규칙 하나 고칠 때마다 전체 재처리가 돌면,
규칙 다섯 개를 손보는 동안 같은 데이터를 다섯 번 다시 쓴다.

## 건드리지 않는 것

`raw_jobs` 는 읽기만 한다. `delivered_at` 도 그대로 둔다 — 소비 측이 이미 가져간 표시를
지우면 같은 데이터가 다시 넘어간다 (`.claude/rules/data-safety.md`). 아래 UPDATE 문이
규칙이 만드는 컬럼과 `parent_company_name`, `source_url`, `normalized_at` 만 적는 것이 그 보장이다.

`job_field_overrides` 도 읽기만 한다. 재정규화는 규칙을 다시 태우는 동작이지 사람이 검수한
값을 지우는 동작이 아니다. 규칙 위에 보정을 덮는 순서는 `app/normalize/engine.py` 가 정한다.

`crawl_runs` 에도 쓰지 않는다. 재정규화는 크롤링 실행이 아니고, 섞어 쓰면 워크플로우의
성공·실패 통계가 크롤링과 무관한 이유로 움직인다.

## 회사 행은 여기서도 생긴다

이미 쌓인 공고에는 이 경로가 유일한 등록 길이다. 아래 UPDATE 는 `insert_normalized` 를 지나지
않으므로 회사 등록을 여기서 한 번 더 부른다 (`app/companies.py`). 부르지 않으면 규칙으로
회사명을 고쳐 재정규화한 뒤에도 새 이름의 행이 없고, 운영자는 로고를 붙일 회사를 화면에서
찾지 못한다.

있는 행은 덮지 않으므로 몇 번을 돌려도 로고와 모회사 이름은 그대로다. 옛 이름의 행은 남는다 —
지우는 것은 운영자가 한다.

## 진행 상황

한 프로세스 안의 메모리에 둔다. 이 서비스는 FastAPI 한 프로세스이고, 재정규화는 그 프로세스가
살아 있는 동안만 도는 작업이라 진행 상황도 그 수명을 넘길 이유가 없다. 프로세스가 죽으면
작업도 같이 죽고, 운영자는 다시 누른다. 이력이 필요해지면 그때 테이블을 만든다.

돌고 있는 동안 들어온 요청은 거부한다. 같은 재정규화가 둘이면 같은 행을 두 번 쓰고, 진행
상황은 어느 쪽 것인지 알 수 없게 된다.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app import companies
from app.normalize.engine import (
    PARENT_COMPANY,
    NormalizeError,
    RawJobMissingError,
    insert_normalized,
    load_rules,
    normalized_values,
    posting_parts,
)
from app.normalize.rules import NORMALIZED_FIELDS, Rule

logger = logging.getLogger(__name__)

ConnectFactory = Callable[[], sqlite3.Connection]

# 실패 사유를 몇 건까지 들고 있을지. 전부 쌓아 두면 규칙 하나가 틀렸을 때 메모리에 만 건이
# 같은 문장으로 들어찬다. 앞의 몇 건이면 무엇이 틀렸는지는 충분히 보인다.
MAX_ERRORS = 20


class BackfillRunningError(RuntimeError):
    """이미 재정규화가 돌고 있다. 두 번 돌리지 않는다."""


@dataclass
class BackfillProgress:
    """대상 건수, 처리 건수, 실패 건수. 화면이 그대로 읽는다."""

    running: bool = False
    total: int = 0
    processed: int = 0
    failed: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    errors: list[str] = field(default_factory=list)

    def snapshot(self) -> BackfillProgress:
        """읽는 쪽에 넘길 복사본. 돌고 있는 작업이 값을 바꿔도 흔들리지 않는다."""
        return BackfillProgress(
            running=self.running,
            total=self.total,
            processed=self.processed,
            failed=self.failed,
            started_at=self.started_at,
            finished_at=self.finished_at,
            errors=list(self.errors),
        )

    def note(self, message: str) -> None:
        self.failed += 1
        if len(self.errors) < MAX_ERRORS:
            self.errors.append(message)


class Backfill:
    """재정규화 한 번. 동시에 하나만 돈다."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._progress = BackfillProgress()
        self._thread: threading.Thread | None = None

    def progress(self) -> BackfillProgress:
        with self._lock:
            return self._progress.snapshot()

    def start(self, connect: ConnectFactory) -> BackfillProgress:
        """백그라운드로 시작한다. 이미 돌고 있으면 `BackfillRunningError`."""
        with self._lock:
            if self._progress.running:
                raise BackfillRunningError("재정규화가 이미 돌고 있다")
            self._progress = BackfillProgress(running=True, started_at=_now())
            started = self._progress.snapshot()

        self._thread = threading.Thread(
            target=self._work, args=(connect,), name="renormalize", daemon=True
        )
        self._thread.start()
        return started

    def wait(self, timeout: float | None = None) -> bool:
        """작업이 끝날 때까지 기다린다. 끝났으면 True. 테스트와 종료 처리가 쓴다."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _work(self, connect: ConnectFactory) -> None:
        conn = connect()
        try:
            renormalize(conn, self._progress)
        except Exception as exc:
            # 스레드에서 올라온 예외는 아무도 보지 못한다. 진행 상황에 남긴다
            logger.exception("재정규화가 중단됐다")
            with self._lock:
                self._progress.note(f"재정규화가 중단됐다: {exc}")
        finally:
            conn.close()
            with self._lock:
                self._progress.running = False
                self._progress.finished_at = _now()


def renormalize(conn: sqlite3.Connection, progress: BackfillProgress) -> BackfillProgress:
    """`raw_jobs` 를 처음부터 다시 읽어 `normalized_jobs` 를 갱신한다.

    한 건이 실패해도 나머지는 계속 간다. 규칙 하나가 어떤 값에서만 터지는 경우가 흔하고,
    거기서 멈추면 뒤쪽 데이터는 손도 못 댄 채 남는다.
    """
    raw_ids = [int(row["id"]) for row in conn.execute("SELECT id FROM raw_jobs ORDER BY id")]
    progress.total = len(raw_ids)

    try:
        rules = load_rules(conn)
    except NormalizeError as exc:
        # 규칙을 못 읽으면 어떤 건도 정규화할 수 없다. 아무것도 쓰지 않고 끝낸다
        progress.note(f"정규화 규칙을 읽지 못했다: {exc}")
        return progress

    for raw_id in raw_ids:
        try:
            rewrite_one(conn, raw_id, rules)
            progress.processed += 1
        except (NormalizeError, RawJobMissingError) as exc:
            progress.note(f"raw_jobs {raw_id}: {exc}")

    logger.info(
        "재정규화: 대상 %s건, 처리 %s건, 실패 %s건",
        progress.total,
        progress.processed,
        progress.failed,
    )
    return progress


def rewrite_one(conn: sqlite3.Connection, raw_job_id: int, rules: list[Rule]) -> None:
    """한 건을 다시 정규화한다. 행이 없으면 새로 넣는다.

    UPDATE 가 적는 컬럼은 `NORMALIZED_FIELDS` 와 `parent_company_name`, `source_url`,
    `normalized_at`
    뿐이다. 주소는 분류가 공고를 나누면 번호가 붙어 달라진다.
    `delivered_at` 은 목록에 없고, 그래서 소비 측이 가져간 표시는 재정규화를 몇 번 돌려도
    그대로다.

    컬럼 목록을 손으로 적지 않는다. `insert_normalized` 와 같은 상수를 봐야 최초 정규화와
    재정규화가 같은 칸을 쓴다 — 0011 이 더한 열 칸이 여기 없어서, 재정규화로는 그 칸이
    영원히 NULL 로 남고 있었다.

    회사 행도 여기서 보장한다. 이 경로는 UPDATE 라 `insert_normalized` 의 등록을 지나지
    않는데, 이미 쌓인 공고에는 재정규화가 유일한 등록 길이다.

    운영자가 `crawlers.default_company` 를 고쳤으면 그 값이 이 경로로 `parent_company_name` 에
    반영된다. 공고에서 뽑은 `company_name` 는 그 영향을 받지 않는다 — 두 칸이 갈린 뒤로 한쪽을
    고치는 일이 다른 쪽을 건드리지 않는다.

    사람이 고친 필드는 규칙을 다시 태워도 사람 값으로 남는다. 규칙이 좋아지는 것은 보정하지
    않은 필드뿐이고, 그것이 검수가 살아남는 유일한 순서다.
    """
    # 컬럼 이름은 이 모듈이 임포트한 상수에서만 온다. 밖에서 오는 값이 들어오지 않는다
    columns = (*NORMALIZED_FIELDS, PARENT_COMPANY)
    # 분류가 공고를 나눴으면 번호마다 한 행이다. 같은 번호의 행을 고친다 — 나누기 전의 1번 행은
    # 그대로 1번 공고가 되고, 그 행의 전달 표시도 따라간다
    for part in posting_parts(conn, raw_job_id):
        source_url, fields = normalized_values(conn, raw_job_id, rules, part)
        companies.register(conn, fields["company_name"], fields[PARENT_COMPANY])
        cursor = conn.execute(
            f"""
            UPDATE normalized_jobs
               SET {", ".join(f"{name} = ?" for name in columns)},
                   source_url = ?,
                   normalized_at = datetime('now')
             WHERE raw_job_id = ? AND part = ?
            """,
            (*(fields[name] for name in columns), source_url, raw_job_id, part.number),
        )
        if cursor.rowcount == 0:
            # 적재는 됐는데 정규화에 실패했던 건이거나, 분류가 새로 나눈 공고다
            insert_normalized(conn, raw_job_id, rules, part)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


_backfill = Backfill()


def get_backfill() -> Backfill:
    """앱이 쓰는 재정규화 하나. 테스트는 이 의존성을 갈아끼운다."""
    return _backfill
