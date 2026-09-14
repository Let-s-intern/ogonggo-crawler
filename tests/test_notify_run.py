"""실행이 끝났을 때 알림을 보내는지 테스트 (1.4.V).

보내지 않아야 하는 경우를 먼저 본다. 신규 0건, 설정값 미만, 꺼져 있음. 새 공고가 아닌
이유로 알림이 오기 시작하면 알림 전체가 읽히지 않는다.

그리고 알림 쪽 사고가 수집을 실패로 만들지 않는지 본다. 부르는 자리가 실행의 끝이다.
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator

import httpx
import pytest

from app import db
from app.notify import new_jobs as run_notify
from app.notify import settings as store
from app.notify.message import NewJob
from app.notify.ntfy import send as real_send


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (id, name, list_url) VALUES (1, 'SK 채용', 'https://sk.example.com')"
    )
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, 'SK 채용')")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """보내진 요청을 담는다. 실제 알림 서버를 때리지 않는다."""
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "abc"})

    transport = httpx.MockTransport(handle)

    async def send(target, message, **kwargs):  # type: ignore[no-untyped-def]
        return await real_send(target, message, transport=transport)

    monkeypatch.setattr(run_notify, "send", send)
    return seen


def turn_on(conn: sqlite3.Connection, *, min_new_count: int = 1) -> None:
    store.write_config(
        conn,
        store.NotifyConfig(
            enabled=True,
            server_url="https://ntfy.example.com",
            topic="job",
            min_new_count=min_new_count,
        ),
    )


def jobs(count: int) -> list[NewJob]:
    return [NewJob(company_name="SK", title=f"공고 {index}") for index in range(count)]


async def test_신규_0건이면_보내지_않는다(
    conn: sqlite3.Connection, sent: list[httpx.Request]
) -> None:
    turn_on(conn)

    result = await run_notify.notify_new_jobs(conn, workflow_id=1, new_count=0, jobs=[])

    assert result is None
    assert sent == []


async def test_설정값_미만이면_보내지_않는다(
    conn: sqlite3.Connection, sent: list[httpx.Request]
) -> None:
    turn_on(conn, min_new_count=5)

    result = await run_notify.notify_new_jobs(conn, workflow_id=1, new_count=4, jobs=jobs(4))

    assert result is None
    assert sent == []


async def test_설정값과_같으면_보낸다(conn: sqlite3.Connection, sent: list[httpx.Request]) -> None:
    """경계. `초과` 로 잘못 쓰면 기준 건수를 딱 맞춘 실행이 조용히 새어 나간다."""
    turn_on(conn, min_new_count=5)

    result = await run_notify.notify_new_jobs(conn, workflow_id=1, new_count=5, jobs=jobs(5))

    assert result is not None
    assert result.ok
    assert len(sent) == 1


async def test_꺼져_있으면_보내지_않는다(
    conn: sqlite3.Connection, sent: list[httpx.Request]
) -> None:
    store.write_config(conn, store.NotifyConfig(enabled=False))

    result = await run_notify.notify_new_jobs(conn, workflow_id=1, new_count=104, jobs=jobs(104))

    assert result is None
    assert sent == []


async def test_저장한_적이_없으면_꺼진_것으로_본다(
    conn: sqlite3.Connection, sent: list[httpx.Request]
) -> None:
    result = await run_notify.notify_new_jobs(conn, workflow_id=1, new_count=3, jobs=jobs(3))

    assert result is None
    assert sent == []


async def test_제목에_워크플로우_이름과_건수가_들어간다(
    conn: sqlite3.Connection, sent: list[httpx.Request]
) -> None:
    turn_on(conn)

    await run_notify.notify_new_jobs(conn, workflow_id=1, new_count=104, jobs=jobs(104))

    assert sent[0].headers["X-Title"] == "SK 채용 새 공고 104건"


async def test_알림_서버가_죽어_있어도_예외가_나가지_않는다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """부르는 자리가 실행의 끝이다. 여기서 예외가 나가면 수집이 실패로 기록된다."""
    turn_on(conn)

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("연결하지 못했다", request=request)

    transport = httpx.MockTransport(broken)

    async def send(target, message, **kwargs):  # type: ignore[no-untyped-def]
        return await real_send(target, message, transport=transport)

    monkeypatch.setattr(run_notify, "send", send)

    result = await run_notify.notify_new_jobs(conn, workflow_id=1, new_count=3, jobs=jobs(3))

    assert result is not None
    assert result.ok is False


async def test_알림_쪽에서_예외가_나도_밖으로_새지_않는다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn_on(conn)

    async def explode(target, message, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("알림 쪽 사고")

    monkeypatch.setattr(run_notify, "send", explode)

    result = await run_notify.notify_new_jobs(conn, workflow_id=1, new_count=3, jobs=jobs(3))

    assert result is None


async def test_워크플로우_이름이_없어도_보낸다(
    conn: sqlite3.Connection, sent: list[httpx.Request]
) -> None:
    turn_on(conn)
    conn.execute("UPDATE workflows SET name = '' WHERE id = 1")

    await run_notify.notify_new_jobs(conn, workflow_id=1, new_count=1, jobs=jobs(1))

    assert len(sent) == 1
    assert "이름 없는 워크플로우" in sent[0].headers["X-Title"]


def test_알림을_누르면_그_사이트의_목록_페이지가_열린다(conn: sqlite3.Connection) -> None:
    """공고 하나가 아니라 목록이다. 한 알림에 여러 건이 들어오므로 하나만 열면 나머지를 놓친다."""
    assert run_notify._list_url(conn, 1) == "https://sk.example.com"


def test_목록_주소가_없으면_빈_값이다(conn: sqlite3.Connection) -> None:
    """없는 워크플로우다. 부르는 쪽이 설정에 적어 둔 주소로 떨어뜨린다."""
    assert run_notify._list_url(conn, 999) == ""
