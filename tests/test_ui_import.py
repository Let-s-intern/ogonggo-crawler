"""운영 설정의 데이터 파일 업로드 화면 (19.3).

병합 규칙 자체는 `tests/test_import_merge.py` 가 본다. 여기서 확인하는 것은 화면이다.

| 확인 | 깨지면 |
|---|---|
| 올리기 전에 무엇이 일어나는지 적혀 있다 | 덮어쓰는 줄 알고 안 누르거나, 눌러 놓고 놀란다 |
| 올리는 동안 버튼이 잠긴다 | 두 번 눌러 같은 파일이 두 번 들어간다 |
| 결과가 항목별 숫자로 나온다 | 무엇이 들어왔는지 화면에서 알 수 없다 |
| 거절 사유가 그대로 나온다 | "실패" 만 보고 다음에 무엇을 할지 모른다 |

파일은 저장소의 실제 스냅샷을 올린다. 이 기능이 실제로 받게 될 파일이다.
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi.testclient import TestClient

from app import db, field_names
from app.api import settings as settings_api
from app.api import workflows as workflows_api
from app.main import app
from app.normalize.rules import NORMALIZED_FIELDS
from app.scheduler import WorkflowScheduler

SNAPSHOT = pathlib.Path(__file__).resolve().parent.parent / "seeds" / "snapshot" / "jobs.db"


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    """빈 서버. 배포 직후의 상태이자 이 기능이 실제로 쓰이는 자리다."""
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def scheduler() -> Iterator[WorkflowScheduler]:
    async def do_nothing(workflow_id: int) -> None:
        return None

    instance = WorkflowScheduler(scheduler=AsyncIOScheduler(timezone="UTC"), runner=do_nothing)
    try:
        yield instance
    finally:
        instance.shutdown()


@pytest.fixture
def client(
    tmp_path: pathlib.Path, conn: sqlite3.Connection, scheduler: WorkflowScheduler
) -> Iterator[TestClient]:
    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(tmp_path / "jobs.db")
        try:
            yield connection
        finally:
            connection.close()

    app.dependency_overrides[settings_api.get_connection] = request_connection
    app.dependency_overrides[workflows_api.get_workflow_scheduler] = lambda: scheduler
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def upload(client: TestClient, path: pathlib.Path) -> str:
    with path.open("rb") as handle:
        response = client.post(
            "/ui/settings/import",
            files={"file": (path.name, handle, "application/octet-stream")},
        )
    assert response.status_code == 200
    return response.text


def count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"])


def test_설정_화면에_올리기_전_안내와_잠기는_버튼이_있다(client: TestClient) -> None:
    # 운영 설정이 하위 메뉴로 갈렸다. 가져오기는 `/settings/import` 다
    body = client.get("/settings/import").text

    assert "더한다" in body
    assert "덮어쓰지 않는다" in body
    assert 'hx-post="/ui/settings/import"' in body
    assert 'hx-encoding="multipart/form-data"' in body
    # 올리는 동안 버튼을 잠근다. 두 번 누르면 같은 파일이 두 번 들어간다
    assert "hx-disabled-elt" in body


def test_스냅샷을_올리면_건수가_화면에_나온다(client: TestClient, conn: sqlite3.Connection) -> None:
    stored = _snapshot_counts()

    body = upload(client, SNAPSHOT)

    assert "성공" in body
    assert "크롤러" in body and "워크플로우" in body
    assert "정규화 규칙" in body and "공고" in body
    assert str(stored["raw_jobs"]) in body
    assert count(conn, "raw_jobs") == stored["raw_jobs"]
    assert count(conn, "crawlers") == stored["crawlers"]
    assert count(conn, "workflows") == stored["workflows"]
    assert count(conn, "normalization_rules") == stored["normalization_rules_kept"]
    # 저쪽 서버의 실행 기록과 전달 표시는 따라오지 않는다
    assert count(conn, "crawl_runs") == 0
    assert count(conn, "normalized_jobs") > 0
    delivered = conn.execute(
        "SELECT count(*) AS n FROM normalized_jobs WHERE delivered_at IS NOT NULL"
    ).fetchone()["n"]
    assert delivered == 0


def test_같은_파일을_두_번_올리면_두_번째는_전부_중복이다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    stored = _snapshot_counts()
    upload(client, SNAPSHOT)
    before = [tuple(row) for row in conn.execute("SELECT * FROM raw_jobs ORDER BY id")]

    body = upload(client, SNAPSHOT)

    assert "성공" in body
    assert count(conn, "raw_jobs") == stored["raw_jobs"]
    assert [tuple(row) for row in conn.execute("SELECT * FROM raw_jobs ORDER BY id")] == before
    assert "이미 있어 건너뜀" in body


def test_잘못된_파일은_사유와_함께_거절된다(
    client: TestClient, conn: sqlite3.Connection, tmp_path: pathlib.Path
) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("이건 DB 가 아니다", encoding="utf-8")

    body = upload(client, path)

    assert "거절" in body
    assert "not_sqlite" in body
    assert "아무것도 들어가지 않았다" in body
    assert count(conn, "raw_jobs") == 0


def test_거절_사유는_무엇이_틀렸는지_이름을_댄다(
    client: TestClient, tmp_path: pathlib.Path
) -> None:
    """앞선 마이그레이션 파일은 다른 사유로 거절된다. 전부 "잘못된 파일" 이 아니다."""
    path = tmp_path / "ahead.db"
    upload_db = db.connect(path)
    db.migrate_up(upload_db)
    upload_db.execute(
        "INSERT INTO schema_migrations (version, name, applied_at)"
        " VALUES ('9999', 'from_the_future', '2030-01-01T00:00:00+00:00')"
    )
    upload_db.close()

    body = upload(client, path)

    assert "ahead_migration" in body
    assert "9999" in body


def test_결과_화면에_이모지가_없다(client: TestClient) -> None:
    """상태는 단어로 적는다 (`.claude/rules/writing.md`)."""
    body = upload(client, SNAPSHOT) + client.get("/settings/import").text

    assert not any(character in body for character in "✅❌⚠\U0001f4dd⭐")


def _snapshot_counts() -> dict[str, int]:
    source = sqlite3.connect(f"file:{SNAPSHOT}?mode=ro", uri=True)
    try:
        counts = {
            table: int(source.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in ("crawlers", "workflows", "normalization_rules", "raw_jobs")
        }
        # 지워진 칸의 규칙은 들어오지 않는다. 이 파일은 0016 이전에 뜬 것이라
        # `department` 규칙 둘이 들어 있다 (`app/api/import_data.py`). 0031 전에 뜬 파일이라 칸
        # 이름도 옛 이름이고, 가져오기가 새 이름으로 옮겨 들이므로 같은 표로 옮겨 센다
        names = [
            str(row[0]) for row in source.execute("SELECT field_name FROM normalization_rules")
        ]
        counts["normalization_rules_kept"] = sum(
            1 for name in names if field_names.field_name(name) in NORMALIZED_FIELDS
        )
        return counts
    finally:
        source.close()
