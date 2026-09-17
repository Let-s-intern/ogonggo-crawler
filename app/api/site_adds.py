"""사이트 추가를 요청 밖에서 돌린다. 창을 닫아도 돌고, 여러 곳을 한꺼번에 걸 수 있다.

2026-09-17 결정(LC-3344). 등록과 시험 수집이 한 곳에 1분 넘게 걸리는데, 창에서 기다리게 하면 한 번에
한 곳밖에 넣지 못했다. 이제 누르면 바로 돌아오고, 진행은 사이트 목록 맨 위에 보인다. 시험 수집이
되면 고른 주기로 자동 수집까지 시작한다 — 시험 결과를 보고 시작을 한 번 더 누르는 단계를 없앴다.

진행 상황은 이 프로세스의 메모리에 둔다. 분류 실행·항목 채우기와 같은 이유다 — FastAPI 한
프로세스이고, 프로세스가 죽으면 작업도 같이 죽는다(`app/classify/batch.py`). 그때 남은 것은 초안
크롤러뿐이라 시험 실행 화면에서 보인다.

**동시에 도는 수는 둘이다.** 목록이 자바스크립트로 그려지는 사이트는 Chromium 을 띄운다. 열 곳을
한꺼번에 걸면 브라우저 열 개가 떠 서버 메모리를 다 쓴다. 나머지는 "기다리는 중" 으로 줄을 선다.
"""

from __future__ import annotations

import asyncio
import itertools
from dataclasses import dataclass
from datetime import UTC, datetime

# 한꺼번에 돌리는 사이트 추가 수
CONCURRENCY = 2

WAITING = "waiting"
FINDING = "finding"
TESTING = "testing"
FAILED = "failed"

STATE_WORDS: dict[str, str] = {
    WAITING: "기다리는 중",
    FINDING: "목록을 읽고 상세 페이지로 가는 길을 찾는 중",
    TESTING: "공고 몇 건을 시험으로 가져오는 중",
    FAILED: "찾지 못했어요",
}


@dataclass
class SiteAdd:
    id: int
    list_url: str
    company: str
    detail_url: str
    interval_minutes: int
    state: str = WAITING
    problem: str = ""
    technical: str = ""
    crawler_id: int | None = None
    matched: int | None = None
    success_count: int | None = None
    created_at: datetime | None = None

    @property
    def running(self) -> bool:
        return self.state != FAILED

    @property
    def state_word(self) -> str:
        return self.problem if self.state == FAILED and self.problem else STATE_WORDS[self.state]


_ids = itertools.count(1)
_adds: dict[int, SiteAdd] = {}
_tasks: set[asyncio.Task[None]] = set()
_semaphore: asyncio.Semaphore | None = None


def new(list_url: str, company: str, detail_url: str, interval_minutes: int) -> SiteAdd:
    add = SiteAdd(
        id=next(_ids),
        list_url=list_url,
        company=company,
        detail_url=detail_url,
        interval_minutes=interval_minutes,
        created_at=datetime.now(UTC),
    )
    _adds[add.id] = add
    return add


def get(add_id: int) -> SiteAdd | None:
    return _adds.get(add_id)


def listed() -> list[SiteAdd]:
    """먼저 건 것부터. 끝나서 사이트가 된 것은 이미 빠져 있다."""
    return sorted(_adds.values(), key=lambda add: add.id)


def forget(add_id: int) -> SiteAdd | None:
    return _adds.pop(add_id, None)


def slot() -> asyncio.Semaphore:
    """이벤트 루프 안에서 처음 부를 때 만든다. 모듈을 읽을 때 만들면 다른 루프에 묶인다."""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(CONCURRENCY)
    return _semaphore


def keep(task: asyncio.Task[None]) -> None:
    """돌고 있는 작업을 붙잡아 둔다. 참조가 없으면 끝나기 전에 가비지 수집될 수 있다."""
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def clear() -> None:
    """테스트가 앞 테스트의 줄과 섞이지 않게 비운다. 동시 실행 자리도 새 루프에서 다시 만든다."""
    global _ids, _semaphore
    _adds.clear()
    _ids = itertools.count(1)
    _semaphore = None
