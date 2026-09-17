"""부가 워크플로우 화면 (5.1.V ~ 5.6.V).

화면이 답해야 하는 것은 다섯이다. 네비게이션에 자리가 있는가, 목록이 없을 때 무엇을 하면
되는지 말하는가, 종류마다 대상 범위가 다르게 나오는가, `all` 이 확인 없이는 저장되지
않는가, 그리고 등록·수정·상태 토글·지금 실행·지우기가 스케줄러까지 가는가.

모델을 부르지 않는다. 실행이 걸리는 자리(`app/api/side.py` 의 `get_start`)를 갈아끼워, 대상이
없는 실행과 가짜 제공자로 도는 실행만 본다 (`.claude/rules/llm.md`: 실제 호출은 여기서 하지
않는다).
"""

from __future__ import annotations

import pathlib
import sqlite3
import time
from collections.abc import Iterator
from functools import partial

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import rules as rules_api
from app.api import side as side_api
from app.api.ui import SETTINGS_NAV
from app.main import app
from app.scheduler import get_scheduler
from app.side import runner, store
from tests.test_classify_run import GOOD, settings_with_key
from tests.test_classify_run import _seed as seed
from tests.test_selector_generator import FakeClient


@pytest.fixture
def path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "jobs.db"


@pytest.fixture
def conn(path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(path)
    db.migrate_up(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def client(path: pathlib.Path, conn: sqlite3.Connection) -> Iterator[TestClient]:
    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(path)
        try:
            yield connection
        finally:
            connection.close()

    app.dependency_overrides[rules_api.get_connection] = request_connection
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _client_with_fake_provider(path: pathlib.Path, texts: tuple[str, ...]) -> Iterator[TestClient]:
    """지금 실행이 실제로 끝까지 돌게 한다. `tests/test_api_side.py` 와 같은 자리다."""

    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(path)
        try:
            yield connection
        finally:
            connection.close()

    app.dependency_overrides[rules_api.get_connection] = request_connection
    app.dependency_overrides[rules_api.get_connect_factory] = lambda: partial(db.connect, path)
    app.dependency_overrides[side_api.get_start] = lambda: partial(
        runner.start, client=FakeClient(*texts), settings=settings_with_key()
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_네비게이션에_자동_분류가_있다() -> None:
    """설정의 시스템 무리에 있다 (`app/api/ui.py` 의 `SETTINGS_SECTIONS`, 2026-09-17 결정)."""
    assert ("/side", "자동 분류") in SETTINGS_NAV


def test_부가_워크플로우_화면이_열리고_네비게이션이_켜진다(client: TestClient) -> None:
    response = client.get("/side")

    assert response.status_code == 200
    assert '<a href="/side" aria-current="page"' in response.text


def test_전달_종류_워크플로우는_쓰지_않는다고_적는다(client: TestClient) -> None:
    """오공고 전송은 분류 끝과 지금 보내기로 한다 (2026-09-15 결정).

    실행해도 아무 일도 없는 종류임을 적는다.
    """
    body = client.get("/side").text

    assert "전달 종류는 쓰지 않는다" in body
    assert 'href="/deliver"' in body


# ---------------------------------------------------------------------------
# 5.2 목록 — 0건일 때와 여러 건일 때


def test_아무것도_없으면_무엇을_하면_되는지_적는다(client: TestClient) -> None:
    response = client.get("/ui/side")

    assert response.status_code == 200
    assert "등록된 부가 워크플로우가 없다" in response.text


def test_만든_워크플로우가_목록에_나온다(client: TestClient, conn: sqlite3.Connection) -> None:
    store.create(conn, kind="classify", name="아직 분류 안 된 것")
    store.create(conn, kind="deliver", name="전달 준비")

    response = client.get("/ui/side")

    assert "아직 분류 안 된 것" in response.text
    assert "전달 준비" in response.text
    # 새로 만든 것은 멈춤이다
    assert response.text.count("멈춤") >= 2


# ---------------------------------------------------------------------------
# 5.3 등록·수정 — 종류마다 대상 범위가 갈리고, 잘못된 값은 사유와 함께 거절된다


def test_분류를_고르면_분류가_받는_범위만_나온다(client: TestClient) -> None:
    response = client.get("/ui/side/new-form", params={"kind": "classify"})

    assert "아직 분류 안 된 것" in response.text
    assert "아직 전달 안 된 것" not in response.text


def test_전달을_고르면_전달이_받는_범위만_나온다(client: TestClient) -> None:
    response = client.get("/ui/side/new-form", params={"kind": "deliver"})

    assert "아직 전달 안 된 것" in response.text
    assert "분류는 했지만 빈 칸이 남은 것" not in response.text


def test_이름이_비면_사유와_함께_거절된다(client: TestClient, conn: sqlite3.Connection) -> None:
    response = client.post(
        "/ui/side",
        data={
            "kind": "classify",
            "name": "   ",
            "trigger_kind": "manual",
            "interval_minutes": "360",
            "target_scope": "unclassified",
            "batch_limit": "50",
        },
    )

    assert response.status_code == 200
    assert "이름이 비어 있다" in response.text
    assert store.list_all(conn) == []


def test_만들면_멈춘_채로_저장된다(client: TestClient, conn: sqlite3.Connection) -> None:
    response = client.post(
        "/ui/side",
        data={
            "kind": "classify",
            "name": "새 분류",
            "trigger_kind": "manual",
            "interval_minutes": "360",
            "target_scope": "unclassified",
            "batch_limit": "50",
        },
    )

    assert response.status_code == 200
    [created] = store.list_all(conn)
    assert created.status == store.PAUSED
    assert "새 분류" in response.text


# ---------------------------------------------------------------------------
# 5.2.1 — 저장이 스케줄러까지 간다


def test_켜면_주기가_스케줄러에_등록된다(client: TestClient, conn: sqlite3.Connection) -> None:
    """새로 만든 것은 멈춤이라 아직 등록되지 않는다. 켜는 순간 등록돼야 한다."""
    response = client.post(
        "/ui/side",
        data={
            "kind": "classify",
            "name": "주기 분류",
            "trigger_kind": "interval",
            "interval_minutes": "45",
            "target_scope": "unclassified",
            "batch_limit": "50",
        },
    )
    assert response.status_code == 200
    [created] = store.list_all(conn)
    assert created.id not in get_scheduler().side_scheduled()

    client.patch(f"/ui/side/{created.id}/status", data={"status": "active"})

    scheduled = get_scheduler().side_scheduled()
    assert scheduled.get(created.id) == 45


def test_멈추면_스케줄러에서_빠진다(client: TestClient, conn: sqlite3.Connection) -> None:
    workflow = store.create(
        conn, kind="classify", name="주기 분류", status="active", trigger_kind="interval"
    )
    get_scheduler().sync(conn)
    assert workflow.id in get_scheduler().side_scheduled()

    response = client.patch(f"/ui/side/{workflow.id}/status", data={"status": "paused"})

    assert response.status_code == 200
    assert workflow.id not in get_scheduler().side_scheduled()


def test_주기를_고치면_스케줄러_잡도_바뀐다(client: TestClient, conn: sqlite3.Connection) -> None:
    workflow = store.create(
        conn, kind="classify", name="주기 분류", status="active", trigger_kind="interval"
    )
    get_scheduler().sync(conn)

    response = client.patch(
        f"/ui/side/{workflow.id}",
        data={
            "name": workflow.name,
            "trigger_kind": "interval",
            "interval_minutes": "7",
            "target_scope": workflow.target_scope,
            "batch_limit": str(workflow.batch_limit),
        },
    )

    assert response.status_code == 200
    assert get_scheduler().side_scheduled().get(workflow.id) == 7


# ---------------------------------------------------------------------------
# 5.4 — `all` 확인 창


def test_전체_다시를_고르면_확인_없이는_저장되지_않는다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    response = client.post(
        "/ui/side",
        data={
            "kind": "classify",
            "name": "전체 재분류",
            "trigger_kind": "manual",
            "interval_minutes": "360",
            "target_scope": "all",
            "batch_limit": "50",
        },
    )

    assert response.status_code == 200
    assert "확인이 필요하다" in response.text
    assert store.list_all(conn) == []  # 저장되지 않았다
    # 두 번째 제출을 위한 숨은 칸이 이미 실려 있다
    assert 'name="confirmed" value="1"' in response.text


def test_확인_창의_건수가_실제_대상과_같다(client: TestClient, conn: sqlite3.Connection) -> None:
    seed(conn)  # 대상이 될 raw_jobs 를 만든다

    response = client.post(
        "/ui/side",
        data={
            "kind": "classify",
            "name": "전체 재분류",
            "trigger_kind": "manual",
            "interval_minutes": "360",
            "target_scope": "all",
            "batch_limit": "50",
        },
    )

    from app.classify.store import ALL, scope_count

    expected = scope_count(conn, ALL)
    assert f"{expected}건" in response.text


def test_확인하고_다시_제출하면_저장된다(client: TestClient, conn: sqlite3.Connection) -> None:
    response = client.post(
        "/ui/side",
        data={
            "kind": "classify",
            "name": "전체 재분류",
            "trigger_kind": "manual",
            "interval_minutes": "360",
            "target_scope": "all",
            "batch_limit": "50",
            "confirmed": "1",
        },
    )

    assert response.status_code == 200
    [created] = store.list_all(conn)
    assert created.target_scope == "all"


def test_전달_종류는_전체_다시를_골라도_확인을_거치지_않는다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """지금은 전달이 실제로 아무것도 보내지 않는다. 위험이 없어 확인을 걸지 않는다."""
    response = client.post(
        "/ui/side",
        data={
            "kind": "deliver",
            "name": "전체 전달",
            "trigger_kind": "manual",
            "interval_minutes": "360",
            "target_scope": "all",
            "batch_limit": "50",
        },
    )

    assert response.status_code == 200
    assert "확인이 필요하다" not in response.text
    [created] = store.list_all(conn)
    assert created.target_scope == "all"


# ---------------------------------------------------------------------------
# 7.4 전달 종류는 지금 실행이 잠겨 있다 — 보내는 코드가 아직 없다


def test_전달_카드는_지금_실행_단추_대신_이유를_보여준다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    workflow = store.create(conn, kind="deliver", name="전달 준비")

    body = client.get("/ui/side").text

    assert "지금 실행 (쓰지 않음)" in body
    assert f'hx-post="/ui/side/{workflow.id}/run"' not in body


def test_분류_카드는_지금_실행_단추가_그대로_있다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    workflow = store.create(conn, kind="classify", name="분류 준비")

    body = client.get("/ui/side").text

    assert f'hx-post="/ui/side/{workflow.id}/run"' in body


# ---------------------------------------------------------------------------
# 5.5 지금 실행과 진행 상황


def test_지금_실행하면_카드가_돌아오고_대상이_없으면_바로_끝난다(
    path: pathlib.Path, conn: sqlite3.Connection
) -> None:
    workflow = store.create(conn, kind="classify", name="빈 대상")
    for client in _client_with_fake_provider(path, (GOOD,)):
        response = client.post(f"/ui/side/{workflow.id}/run")

        assert response.status_code == 200
        assert f'id="side-row-{workflow.id}"' in response.text

        recruitment_end_at = time.monotonic() + 10
        card = client.get(f"/ui/side/{workflow.id}/card")
        while "실행 중" in card.text and time.monotonic() < recruitment_end_at:
            time.sleep(0.05)
            card = client.get(f"/ui/side/{workflow.id}/card")
        assert "성공" in card.text or "기록 없음" in card.text


def test_돌고_있는_카드는_스스로_폴링을_건다(client: TestClient, conn: sqlite3.Connection) -> None:
    """실행 중인 카드는 스스로 2초마다 다시 부른다.

    2026-08-29 실제 `POST /run` 뒤 즉시 확인하는 방식에서 이 방식으로 바꿨다. 배경 스레드가
    끝나는 속도는 환경(특히 CI 러너)마다 달라, 매 요청 직후 "아직 돌고 있다" 를 확인하려던
    원래 방식은 스레드가 그 사이 이미 끝나 버리면 실패로 보였다 — 열린 실행 행을 직접 만들어
    `running` 상태 자체를 확정하면 이 화면 조각의 렌더링만 결정적으로 본다. 실제 실행이
    끝까지 성공으로 도는지는 `test_지금_실행하면_끝까지_성공으로_닫힌다` 가 따로 확인한다.
    """
    from app.side import runs as runs_module

    workflow = store.create(conn, kind="classify", name="분류 중")
    runs_module.start(conn, workflow.id, trigger="manual")

    card = client.get(f"/ui/side/{workflow.id}/card")

    assert "every 2s" in card.text


def test_지금_실행하면_끝까지_성공으로_닫힌다(path: pathlib.Path, conn: sqlite3.Connection) -> None:
    """`POST /run` 이 배경 스레드에서 끝까지 돌아 성공으로 닫히는지 본다.

    완료 속도가 환경마다 다르므로 즉시 확인하지 않고 끝날 때까지 짧게 폴링한다
    (`test_지금_실행하면_카드가_돌아오고_대상이_없으면_바로_끝난다` 와 같은 방식).
    """
    seed(conn)
    workflow = store.create(conn, kind="classify", name="분류 중")
    for client in _client_with_fake_provider(path, (GOOD, GOOD, GOOD)):
        client.post(f"/ui/side/{workflow.id}/run")

        recruitment_end_at = time.monotonic() + 10
        card = client.get(f"/ui/side/{workflow.id}/card")
        while "실행 중" in card.text and time.monotonic() < recruitment_end_at:
            time.sleep(0.05)
            card = client.get(f"/ui/side/{workflow.id}/card")
        assert "성공" in card.text, card.text


# ---------------------------------------------------------------------------
# 5.6 실행 이력 — 성공·실패·건너뜀이 갈려 보인다


def test_실행_이력에_건너뜀이_사유와_함께_보인다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    from app.side import runs as runs_module

    workflow = store.create(conn, kind="classify", name="겹침 방지 확인")
    runs_module.start(conn, workflow.id, trigger="manual")
    runs_module.skipped(conn, workflow.id, "manual", "이미 돌고 있어 건너뛰었다")

    response = client.get(f"/ui/side/{workflow.id}/card")

    assert "건너뜀" in response.text
    assert "이미 돌고 있어 건너뛰었다" in response.text


def test_실행_중에는_실행_이력_details가_보존_속성을_갖는다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """실행 이력을 여는 details 가 폴링 때마다 새로 그려지면 2초마다 저절로 닫힌다.

    `hx-preserve` 로 기존 노드를 그대로 지켜야 사람이 열어 둔 상태가 유지된다.
    """
    from app.side import runs as runs_module

    workflow = store.create(conn, kind="classify", name="폴링 확인")
    runs_module.start(conn, workflow.id, trigger="manual")  # 열린 실행 -> running=True

    response = client.get(f"/ui/side/{workflow.id}/card")

    assert f'id="side-history-{workflow.id}"' in response.text
    assert 'hx-preserve="true"' in response.text
    assert "every 2s" in response.text  # 도는 동안 폴링이 걸려 있다


def test_실행_이력에_실패_사유가_보인다(client: TestClient, conn: sqlite3.Connection) -> None:
    from app.side import runs as runs_module

    workflow = store.create(conn, kind="classify", name="실패 확인")
    run_id = runs_module.start(conn, workflow.id, trigger="manual")
    runs_module.finish(conn, run_id, status=runs_module.FAILED, error_message="제공자 키가 없다")

    response = client.get(f"/ui/side/{workflow.id}/card")

    assert "실패" in response.text
    assert "제공자 키가 없다" in response.text


# ---------------------------------------------------------------------------
# 지우기 — 목록에서 사라지고 스케줄러 잡도 빠진다


def test_지우면_목록에서_사라지고_스케줄러_잡도_빠진다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    workflow = store.create(
        conn, kind="classify", name="지울 것", status="active", trigger_kind="interval"
    )
    get_scheduler().sync(conn)
    assert workflow.id in get_scheduler().side_scheduled()

    response = client.delete(f"/ui/side/{workflow.id}")

    assert response.status_code == 200
    assert "지울 것" not in response.text
    assert workflow.id not in get_scheduler().side_scheduled()
