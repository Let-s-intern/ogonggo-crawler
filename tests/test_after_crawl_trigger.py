"""크롤 실행 끝에서 `after_crawl` 분류가 이어지는지 (Push 6).

| 확인 | 깨지면 |
|---|---|
| 새 공고가 있으면 활성 `after_crawl` 분류가 걸린다 | 분류를 돌리려면 매번 손으로 눌러야 한다 |
| 신규 0건이면 아무것도 하지 않는다 | 대상 없는 실행이 사이트 수만큼 `side_runs` 에 쌓인다 |
| 분류 쪽이 실패해도 크롤 실행은 성공으로 남는다 | 분류 사고 하나가 수집 전체를 실패로 보이게 한다 |
| `side_runs.trigger` 에 `after_crawl` 이 남는다 | 주기로 돈 것과 갈리지 않아 "실제로 도는가" 에 |
|                                                | 답할 수 없다 |

실사이트에 나가지 않는다. `stub_fetcher` 로 크롤을 돌리고 실제 모델은 부르지 않는다.
"""

from __future__ import annotations

import pathlib
import sqlite3
import time
from collections.abc import Iterator

import pytest

from app.config import get_settings
from app.crawler.runner import run_workflow
from app.side import runner as side_runner
from app.side import runs, store
from tests.test_classify_run import GOOD, settings_with_key
from tests.test_company_selector import WITH_COMPANY, make_conn, stub_fetcher
from tests.test_selector_generator import FakeClient


@pytest.fixture
def path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "jobs.db"


@pytest.fixture
def conn(path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = make_conn(path, WITH_COMPANY, default_company="테스트")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(autouse=True)
def same_db_for_background_thread(
    path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """`start()` 가 여는 배경 스레드는 `db.connect()` 를 인자 없이 부른다.

    인자가 없으면 `get_settings().database_path` 로 간다 (`app/db.py`). 테스트 파일과
    같은 파일을 가리키게 환경변수로 맞춘다 — 안 맞추면 스레드가 운영 경로의 DB 를 열어
    "no such table" 로 조용히 실패한다.
    """
    monkeypatch.setenv("DATABASE_PATH", str(path))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def wait_until_closed(
    conn: sqlite3.Connection, side_workflow_id: int, *, timeout: float = 10
) -> None:
    recruitment_end_at = time.monotonic() + timeout
    while time.monotonic() < recruitment_end_at:
        if runs.open_run(conn, side_workflow_id) is None:
            return
        time.sleep(0.02)
    raise AssertionError("배경 실행이 시간 안에 끝나지 않았다")


async def test_새_공고가_있으면_활성_after_crawl_분류가_걸린다(
    conn: sqlite3.Connection,
) -> None:
    workflow = store.create(
        conn, kind="classify", name="수집 직후 분류", status="active", trigger_kind="after_crawl"
    )
    conn.commit()

    result = await run_workflow(conn, 1, fetcher=stub_fetcher(), limit=2)
    assert result.new_count > 0

    wait_until_closed(conn, workflow.id)
    [run] = runs.recent(conn, workflow.id)
    assert run.trigger == "after_crawl"
    # 성공 여부는 보지 않는다 — 실제 제공자 키가 없는 환경에서는 `failed` 로 닫히는 것이
    # 맞는 동작이다. 여기서 보는 것은 "걸렸는가" 다. 실제로 도는지는 가짜 제공자로 따로 본다
    assert run.status is not None


async def test_신규_0건이면_아무것도_하지_않는다(conn: sqlite3.Connection) -> None:
    workflow = store.create(
        conn, kind="classify", name="수집 직후 분류", status="active", trigger_kind="after_crawl"
    )
    conn.commit()

    # 먼저 한 번 돌려 전부 아는 공고로 만든 뒤, 두 번째 실행은 신규가 0건이다
    await run_workflow(conn, 1, fetcher=stub_fetcher(), limit=2)
    wait_until_closed(conn, workflow.id)

    before = len(runs.recent(conn, workflow.id, limit=10))
    result = await run_workflow(conn, 1, fetcher=stub_fetcher(), limit=2)

    assert result.new_count == 0
    # 두 번째 실행에서 새로 걸린 행이 없다 — 개수가 그대로다
    assert len(runs.recent(conn, workflow.id, limit=10)) == before


async def test_멈춘_워크플로우는_걸리지_않는다(conn: sqlite3.Connection) -> None:
    workflow = store.create(
        conn, kind="classify", name="꺼진 분류", status="paused", trigger_kind="after_crawl"
    )
    conn.commit()

    await run_workflow(conn, 1, fetcher=stub_fetcher(), limit=2)

    assert runs.recent(conn, workflow.id, limit=10) == []


async def test_수동_시점의_워크플로우는_걸리지_않는다(conn: sqlite3.Connection) -> None:
    """`after_crawl` 이 아니면 크롤이 끝나도 스스로 돌지 않는다."""
    workflow = store.create(
        conn, kind="classify", name="수동 분류", status="active", trigger_kind="manual"
    )
    conn.commit()

    await run_workflow(conn, 1, fetcher=stub_fetcher(), limit=2)

    assert runs.recent(conn, workflow.id, limit=10) == []


def test_분류_쪽_사고가_나도_예외를_올리지_않는다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`trigger_after_crawl` 은 크롤 실행의 끝에서 부른다. 여기서 예외가 나가면 안 된다."""
    store.create(
        conn, kind="classify", name="사고나는 분류", status="active", trigger_kind="after_crawl"
    )
    conn.commit()

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("분류 걸다가 문제 생김")

    monkeypatch.setattr(side_runner, "start", boom)

    # 예외 없이 돌아온다 — 그것이 이 테스트의 전부다
    side_runner.trigger_after_crawl(conn, new_count=1)


def test_적재_0건이면_시작조차_부르지_않는다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.create(
        conn, kind="classify", name="수집 직후 분류", status="active", trigger_kind="after_crawl"
    )
    conn.commit()

    called: list[int] = []

    def spy(*_args: object, **_kwargs: object) -> None:
        called.append(1)

    monkeypatch.setattr(side_runner, "start", spy)

    side_runner.trigger_after_crawl(conn, new_count=0)

    assert called == []


async def test_실제로_분류가_돈다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """가짜 제공자로 끝까지 돌려, 카운트가 진짜로 채워지는지 본다."""
    from functools import partial

    monkeypatch.setattr(
        side_runner,
        "start",
        partial(side_runner.start, client=FakeClient(GOOD, GOOD), settings=settings_with_key()),
    )
    workflow = store.create(
        conn, kind="classify", name="수집 직후 분류", status="active", trigger_kind="after_crawl"
    )
    conn.commit()

    await run_workflow(conn, 1, fetcher=stub_fetcher(), limit=2)

    wait_until_closed(conn, workflow.id)
    [run] = runs.recent(conn, workflow.id)
    assert run.status == runs.SUCCESS
    assert run.processed_count == 2
