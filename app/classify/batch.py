"""분류 실행. 보낼 글이 있고 아직 분류되지 않은 공고를 찾아 돈다.

**수집과 따로 돈다.** 같은 실행에서 이어 돌리면 SK 103건일 때 실행이 9분 가까이 길어지고,
분류가 실패하면 수집까지 실패로 보인다. 수집은 본문까지만 하고, 나누는 것은 여기가 한다
(`.claude/tasks/memos/보류/llm-classify/prd-llm-classify.md`).

**분류 실패는 그 공고에서 끝난다.** 그 공고는 본문만 가진 채로 남고 `job_classifications` 에
행이 생기지 않아서, 다음 실행이 다시 집어 든다. 본문이 `raw_jobs` 에 있으니 몇 번이든 다시
돌릴 수 있다.

**한 번에 도는 건수에 상한이 있다.** 640건을 한 번에 돌리면 약 285만 토큰이 실제로 나가고,
돌기 시작하면 멈출 수가 없다. 상한을 넘겨 부르면 상한으로 깎는다.

진행 상황은 한 프로세스 안의 메모리에 둔다. 재정규화와 같은 이유다 — 이 서비스는 FastAPI
한 프로세스이고, 프로세스가 죽으면 작업도 같이 죽는다 (`app/normalize/backfill.py`).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app import taxonomy
from app.classify.classifier import ClassifyError, chosen, classify_body
from app.classify.schema import build_classification_model
from app.classify.store import (
    pending_count,
    pending_ids,
    read_current_values,
    read_parts,
    read_source,
    read_title,
    save_classification,
    save_suggestions,
)
from app.config import Settings
from app.llm import settings as llm_settings
from app.llm.base import Usage
from app.llm.log import CLASSIFY, record_call
from app.normalize.backfill import ConnectFactory, rewrite_one
from app.normalize.engine import NormalizeError, load_rules

logger = logging.getLogger(__name__)

# 한 번에 도는 건수. 기본은 보수적으로(적게) 둔다
DEFAULT_LIMIT = 50
# 한 번에 도는 건수의 상한. 이보다 큰 값을 주면 여기로 깎는다
MAX_LIMIT = 200

# 실패 사유를 몇 건까지 들고 있을지. 전부 쌓으면 키 하나가 틀렸을 때 메모리에 같은 문장이
# 수백 줄 들어찬다
MAX_ERRORS = 20


class ClassifyRunningError(RuntimeError):
    """이미 분류가 돌고 있다. 두 번 돌리지 않는다."""


@dataclass
class ClassifyProgress:
    """대상 건수, 처리 건수, 실패 건수, 그리고 이번 실행이 쓴 토큰."""

    running: bool = False
    total: int = 0
    processed: int = 0
    failed: int = 0
    # 모델이 냈지만 본문에서 찾지 못해 버린 칸의 수. 지어낸 값이 얼마나 나오는지는 세어야 안다
    dropped: int = 0
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    errors: list[str] = field(default_factory=list)

    def snapshot(self) -> ClassifyProgress:
        """읽는 쪽에 넘길 복사본. 돌고 있는 작업이 값을 바꿔도 흔들리지 않는다."""
        return ClassifyProgress(**{**vars(self), "errors": list(self.errors)})

    def note(self, message: str) -> None:
        self.failed += 1
        if len(self.errors) < MAX_ERRORS:
            self.errors.append(message)

    def count(self, usage: Usage) -> None:
        self.calls += 1
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.total_tokens += usage.total_tokens


class ClassifyRun:
    """분류 한 번. 동시에 하나만 돈다."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._progress = ClassifyProgress()
        self._thread: threading.Thread | None = None

    def progress(self) -> ClassifyProgress:
        with self._lock:
            return self._progress.snapshot()

    def start(self, connect: ConnectFactory, limit: int = DEFAULT_LIMIT) -> ClassifyProgress:
        """백그라운드로 시작한다. 이미 돌고 있으면 `ClassifyRunningError`."""
        with self._lock:
            if self._progress.running:
                raise ClassifyRunningError("분류가 이미 돌고 있다")
            self._progress = ClassifyProgress(running=True, started_at=_now())
            started = self._progress.snapshot()

        self._thread = threading.Thread(
            target=self._work, args=(connect, bounded(limit)), name="classify", daemon=True
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

    def claim(self) -> ClassifyProgress:
        """분류 자리만 잡는다. 이미 돌고 있으면 `ClassifyRunningError`.

        원문 다시 수집이 워크플로우 공고를 다시 분류할 때 쓴다 (`app/crawler/recollect.py`).
        잡는 동안 `POST /api/classify` 와 부가 워크플로우의 분류가 물러난다 — 같은 공고에 두 번
        돈을 쓰지 않는다. 돌려주는 진행 상황을 `classify_ids` 에 그대로 넘기고, 끝나면
        `release()` 를 부른다.
        """
        with self._lock:
            if self._progress.running:
                raise ClassifyRunningError("분류가 이미 돌고 있다")
            self._progress = ClassifyProgress(running=True, started_at=_now())
            return self._progress

    def release(self) -> None:
        """`claim()` 으로 잡은 자리를 놓는다."""
        with self._lock:
            self._progress.running = False
            self._progress.finished_at = _now()

    def _work(self, connect: ConnectFactory, limit: int) -> None:
        conn = connect()
        try:
            asyncio.run(classify_pending(conn, self._progress, limit=limit))
        except Exception as exc:
            # 스레드에서 올라온 예외는 아무도 보지 못한다. 진행 상황에 남긴다
            logger.exception("분류가 중단됐다")
            with self._lock:
                self._progress.note(f"분류가 중단됐다: {exc}")
        finally:
            conn.close()
            with self._lock:
                self._progress.running = False
                self._progress.finished_at = _now()


def bounded(limit: int) -> int:
    """한 번에 도는 건수를 상한 안으로 깎는다. 거절하지 않고 깎는다."""
    return max(1, min(limit, MAX_LIMIT))


async def classify_pending(
    conn: sqlite3.Connection,
    progress: ClassifyProgress,
    *,
    limit: int = DEFAULT_LIMIT,
    client: Any | None = None,
    settings: Settings | None = None,
) -> ClassifyProgress:
    """원문이나 본문이 있고 아직 분류되지 않은 공고를 상한만큼 돈다."""
    return await classify_ids(
        conn,
        pending_ids(conn, bounded(limit)),
        progress,
        client=client,
        settings=settings,
    )


async def classify_ids(
    conn: sqlite3.Connection,
    raw_job_ids: list[int],
    progress: ClassifyProgress,
    *,
    client: Any | None = None,
    settings: Settings | None = None,
) -> ClassifyProgress:
    """정해진 공고만 분류한다. 표본 실행이 쓰는 자리이기도 하다.

    한 건이 실패해도 나머지는 계속 간다. 실패한 공고는 `job_classifications` 에 행이 생기지
    않아서 다음 실행이 다시 집어 든다.
    """
    progress.total = len(raw_job_ids)
    # 화면에서 고른 제공자와 모델이 여기서 들어온다. 실행할 때마다 다시 읽으므로 배포 없이
    # 다음 실행부터 바뀐다 (`app/llm/settings.py`)
    resolved = llm_settings.settings_for(conn, CLASSIFY, settings)

    try:
        # 제공자와 모델을 여기서 먼저 읽는다. 실패한 호출도 기록해야 하는데, 그때는
        # 응답이 없어 누가 무엇으로 실패했는지 알 길이 이것뿐이다
        provider, model = chosen(resolved)
        resolved_client = client or _client(resolved)
        rules = load_rules(conn)
    except (ClassifyError, NormalizeError) as exc:
        # 클라이언트를 못 만들거나 규칙을 못 읽으면 어떤 건도 처리할 수 없다. 아무것도 쓰지
        # 않고 끝낸다
        progress.note(str(exc))
        return progress

    # 직무 분류 표는 배치 시작 전에 한 번만 읽는다. 공고 640건을 돈다고 표를 640번 읽을
    # 이유가 없다 — 표는 이 실행 도중에는 바뀌지 않는다고 본다
    taxonomy_tree = taxonomy.enabled_tree(conn)
    response_model = build_classification_model(conn)

    for raw_job_id in raw_job_ids:
        # 원문이 있으면 원문, 없으면 본문이다. 옛 건에는 원문이 없다 (`app/classify/store.py`)
        source = read_source(conn, raw_job_id)
        # 제목은 `job_role` 의 출처다. 본문만 보내면 그 칸이 영원히 빈다
        title = read_title(conn, raw_job_id)
        # company·deadline·start_date 중 수집이 이미 채운 값. 무엇이 채워져 있는지 몰라서는
        # 분류가 원문과 "다르다" 를 말할 수 없다
        current_values = read_current_values(conn, raw_job_id)

        # 이미 나눈 공고는 나눈 목록을 그대로 두고 칸만 다시 채운다 (2026-09-11 결정). 번호에
        # 사람 보정과 전달된 공고 주소가 붙어 있어 개수나 순서가 바뀌면 그 값이 다른 직무로
        # 옮겨 붙는다. 한 번도 나누지 않은 공고(1번 하나, 보낸 줄 없음)는 나눌 수 있다
        stored = read_parts(conn, raw_job_id)
        known = stored if len(stored) > 1 or any(part.lines for part in stored) else []

        def counted(usage: Usage) -> None:
            # 호출 하나가 행 하나다. 깨진 응답으로 한 번 더 물었으면 두 행이 남는다
            progress.count(usage)
            record_call(conn, feature=CLASSIFY, usage=usage)

        try:
            result = await classify_body(
                source,
                title=title,
                current_values=current_values,
                taxonomy_tree=taxonomy_tree,
                response_model=response_model,
                known_parts=[(part.role, part.lines) for part in known],
                settings=resolved,
                client=resolved_client,
                on_call=counted,
            )
        except ClassifyError as exc:
            _note_failed_call(conn, provider.name, model, exc)
            progress.note(f"raw_jobs {raw_job_id}: {exc}")
            continue

        # 직무마다 나뉘었으면 번호마다 한 행이다. 나누지 않은 공고는 1번 하나이고 직무 이름을
        # 따로 남기지 않는다 — 그 직무는 제목에서 온 `job_role` 그대로다
        # 목록을 고정한 공고는 직무 이름도 저장된 것을 쓴다
        split = len(known) > 1 if known else result.split
        for part, posting in enumerate(result.postings, start=1):
            role = known[part - 1].role if known else posting.fields.get("job_role", "").strip()
            save_classification(
                conn,
                raw_job_id,
                posting.fields,
                model=result.usage.model,
                dropped=posting.dropped,
                evidence=posting.evidence,
                part=part,
                part_role=(role or None) if split else None,
                part_lines=posting.sent_lines,
            )
            progress.dropped += len(posting.dropped)
            # 같은 호출의 다른 갈래다. 값이 있는 칸에 원문이 다른 값을 낸 것은 여기로 간다 —
            # `normalize/engine.py` 는 이 표를 읽지 않는다 (PRD 6절). 회사명·마감일은 공고 한
            # 건 전체의 값이라 나눈 공고마다 같은 제안이 붙는다
            save_suggestions(
                conn, raw_job_id, result.suggestions, result.suggestion_reasons, part=part
            )
        try:
            # 분류가 채운 칸이 `normalized_jobs` 까지 가야 소비 측이 본다. 규칙 -> 분류 ->
            # 사람 보정 순서는 정규화 경로 하나가 정한다 (`app/normalize/engine.py`)
            rewrite_one(conn, raw_job_id, rules)
        except NormalizeError as exc:
            # 분류는 남았다. 규칙을 고쳐 재정규화하면 그때 반영된다
            progress.note(f"raw_jobs {raw_job_id}: 분류는 저장했으나 정규화가 실패했다: {exc}")
            continue
        progress.processed += 1

    logger.info(
        "분류: 대상 %s건, 처리 %s건, 실패 %s건, 버린 칸 %s개, 호출 %s회, 토큰 %s",
        progress.total,
        progress.processed,
        progress.failed,
        progress.dropped,
        progress.calls,
        progress.total_tokens,
    )
    return progress


def remaining(conn: sqlite3.Connection) -> int:
    """아직 분류되지 않은 공고 수. 화면이 "몇 건 남았나" 로 읽는다."""
    return pending_count(conn)


def _client(settings: Settings) -> Any:
    from app.classify.classifier import build_client

    return build_client(settings)


def _note_failed_call(
    conn: sqlite3.Connection, provider: str, model: str, exc: ClassifyError
) -> None:
    """응답을 받지 못한 호출도 남긴다. 토큰은 알 수 없어 0 이다.

    `empty_body` 는 모델을 부르지 않은 것이라 남기지 않는다 — 부르지 않은 호출을 기록하면
    호출 수가 실제보다 많아진다. `parts_mismatch` 는 모델이 답한 뒤의 거절이라 그 호출은 이미
    남았다.
    """
    if exc.reason in ("empty_body", "parts_mismatch"):
        return
    record_call(
        conn,
        feature=CLASSIFY,
        usage=Usage(
            provider=provider,
            model=model,
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            latency_ms=0,
        ),
        ok=False,
        error=f"{exc.reason}: {exc}",
    )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


_run = ClassifyRun()


def get_classify_run() -> ClassifyRun:
    """앱이 쓰는 분류 실행 하나. 테스트는 이 의존성을 갈아끼운다."""
    return _run
