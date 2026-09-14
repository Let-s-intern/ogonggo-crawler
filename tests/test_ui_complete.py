"""완성 공고 화면.

실사이트에 나가지 않는다. 저장된 행을 넣고 화면 경로로만 연다.

| 확인 | 깨지면 |
|---|---|
| 네비게이션에 완성 공고가 있다 | 화면을 찾을 방법이 없다 |
| 60% 이상 찬 건만 나온다 | 완성 기준이 거짓말이 된다 |
| 60% 미만이면 빠진다 | 미완성 건이 완성으로 보인다 |
| 40% 안쪽으로 비어도 완성이다 | 100% 를 요구해 통과하는 건이 지나치게 적어진다 |
| 카드가 읽기 전용 미리보기를 연다 | 이 화면에서 고칠 수 있게 보인다 |
| 로고가 있으면 카드·미리보기에 나온다 | 소비 측이 볼 모양과 다르게 보인다 |
| 미리보기에 본문 섹션이 나온다 | 본문을 확인할 방법이 없다 |
| 다음 커서가 마지막 id 다 | 스크롤을 내려도 다음 묶음이 안 온다 |
| 더 가져올 것이 없으면 감지기가 없다 | 바닥에서 빈 요청을 반복한다 |
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api.settings import get_connection
from app.api.ui import NAV_GROUPS
from app.main import app
from app.normalize.rules import NORMALIZED_FIELDS

LIST_URL = "https://example.test/jobs/"


def insert_job(
    conn: sqlite3.Connection,
    raw_job_id: int,
    *,
    complete: bool,
    job_field: str = "IT·개발",
    blank_count: int = 0,
) -> None:
    """`blank_count` 만큼 칸을 비운다. `complete=False` 면 60% 미만이 되도록 넉넉히 비운다."""
    conn.execute(
        """
        INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)
        VALUES (?, 1, ?, '{}', ?)
        """,
        (raw_job_id, f"{LIST_URL}{raw_job_id}/", f"hash-{raw_job_id}"),
    )
    values = {name: f"값-{name}" for name in NORMALIZED_FIELDS}
    values["job_field"] = job_field
    values["title"] = f"공고 {raw_job_id}"
    values["company_name"] = "엘지전자"
    # title/company/job_role/job_major 는 카드 확인에 쓰므로 비우지 않는다. 0033 으로 칸이
    # 스물셋이 되어 60% 는 13.8 -> 14칸 이상이 있어야 완성이다. 열 칸을 비우면 열세
    # 칸(56.5%)만 남아 미완성이 된다
    blankable = [
        "recruitment_end_at",
        "body",
        "qualifications",
        "recruitment_start_at",
        "responsibilities",
        "preferred_qualifications",
        "hiring_process",
        "recruitment_notice",
        "region",
        "experience_type",
        "employment_type",
        "job_role",
    ]
    blanks = 10 if not complete else blank_count
    for name in blankable[:blanks]:
        values[name] = ""
    columns = list(NORMALIZED_FIELDS)
    conn.execute(
        f"""
        INSERT INTO normalized_jobs
               (raw_job_id, source_url, parent_company_name, {", ".join(columns)})
        VALUES (?, ?, 'LG', {", ".join("?" for _ in columns)})
        """,
        (raw_job_id, f"{LIST_URL}{raw_job_id}/", *(values[name] for name in columns)),
    )


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status, default_company)"
        " VALUES ('lg', ?, 'promoted', 'LG')",
        (LIST_URL,),
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, 'lg')")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def client(tmp_path: pathlib.Path, conn: sqlite3.Connection) -> Iterator[TestClient]:
    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(tmp_path / "jobs.db")
        try:
            yield connection
        finally:
            connection.close()

    app.dependency_overrides[get_connection] = request_connection
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_네비게이션에_완성_공고가_있다() -> None:
    group = next(members for path, label, members in NAV_GROUPS if label == "데이터 확인")
    assert ("/complete", "완성 공고") in group


def test_화면이_열리고_목록_조각을_부른다(client: TestClient) -> None:
    response = client.get("/complete")

    assert response.status_code == 200
    assert 'hx-get="/ui/complete"' in response.text


def test_완성된_건만_나온다(client: TestClient, conn: sqlite3.Connection) -> None:
    insert_job(conn, 1, complete=True)
    insert_job(conn, 2, complete=False)

    body = client.get("/ui/complete").text

    assert "공고 1" in body
    assert "공고 2" not in body


def test_마흔프로_안쪽으로_비어도_완성으로_본다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """스물세 칸 중 여덟만 비면(15/23 = 65.2%) 여전히 완성이다."""
    insert_job(conn, 1, complete=True, blank_count=8)

    body = client.get("/ui/complete").text

    assert "공고 1" in body


def test_비율이_기준_밑이면_빠진다(client: TestClient, conn: sqlite3.Connection) -> None:
    """열이 비면(13/23 = 56.5%) 60% 기준에 못 미쳐 빠진다."""
    insert_job(conn, 1, complete=False)

    body = client.get("/ui/complete").text

    assert "공고 1" not in body


def test_카드가_읽기_전용_미리보기를_연다(client: TestClient, conn: sqlite3.Connection) -> None:
    insert_job(conn, 1, complete=True)

    body = client.get("/ui/complete").text

    assert "data-modal-open" in body
    assert 'hx-get="/ui/complete/1/preview"' in body
    assert 'hx-target="#app-modal-body"' in body


def test_로고가_있으면_카드에_나온다(client: TestClient, conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO companies (name, logo_url) VALUES ('엘지전자', 'https://logo.test/lg.png')"
    )
    insert_job(conn, 1, complete=True)

    body = client.get("/ui/complete").text

    assert 'src="https://logo.test/lg.png"' in body


def test_카드_태그는_직무_대신_직무_분류를_보여준다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """PRD 대분류·소분류가 카드 태그다. 0031 전에 태그로 쓰던 자유 글자 직무는 지웠다."""
    insert_job(conn, 1, complete=True, job_field="IT·개발")

    body = client.get("/ui/complete").text

    assert "#IT·개발" in body
    assert "#값-job_role" in body


def test_미리보기에_섹션과_본문이_나온다(client: TestClient, conn: sqlite3.Connection) -> None:
    insert_job(conn, 1, complete=True)

    body = client.get("/ui/complete/1/preview").text

    assert "공고 1" in body
    assert "주요 업무" in body
    assert "값-responsibilities" in body
    assert "원문 본문 전체 보기" in body
    assert "값-body" in body
    assert "data-modal-close" in body


def test_없는_공고를_미리보면_사유를_적는다(client: TestClient, conn: sqlite3.Connection) -> None:
    """`/ui/*` 조각은 200으로 돌아오고 안에 사유를 적는다(다른 조각 라우트와 같은 규칙)."""
    response = client.get("/ui/complete/999/preview")

    assert response.status_code == 200
    assert "찾을 수 없다" in response.text


def test_다음_커서가_마지막_id다(client: TestClient, conn: sqlite3.Connection) -> None:
    for i in range(1, 4):
        insert_job(conn, i, complete=True)

    body = client.get("/ui/complete", params={}).text
    # PAGE_SIZE(20) 보다 적게 넣었으니 다음 묶음이 없다
    assert "complete-sentinel" not in body


def test_더_가져올_것이_없으면_감지기가_없고_있으면_생긴다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    from app.api.ui_complete import PAGE_SIZE

    for i in range(1, PAGE_SIZE + 2):
        insert_job(conn, i, complete=True)

    first = client.get("/ui/complete").text
    assert "complete-sentinel" in first
    # 21건 중 최신 20건(id 21..2)이 첫 페이지고, 다음 커서는 그중 가장 작은 id(2)다
    assert "/ui/complete?after=2" in first

    second = client.get("/ui/complete", params={"after": 2}).text
    assert "complete-sentinel" not in second


def test_커서_뒤로는_그_id보다_작은_것만_온다(client: TestClient, conn: sqlite3.Connection) -> None:
    for i in range(1, 4):
        insert_job(conn, i, complete=True)

    body = client.get("/ui/complete", params={"after": 2}).text

    assert "공고 1" in body
    assert "공고 2" not in body
    assert "공고 3" not in body


def test_완성된_건이_없으면_안내를_적는다(client: TestClient) -> None:
    body = client.get("/ui/complete").text

    assert "60% 이상 채워진 공고가 없다" in body
