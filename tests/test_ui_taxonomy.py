"""직무 분류 화면 — 왼쪽 대분류, 오른쪽 소분류 칩, 수정 → 저장 (2026-09-17, LC-3344).

| 확인 | 깨지면 |
|---|---|
| 대분류를 고르면 그 소분류가 칩으로 보인다 | 단계가 한눈에 안 보인다 |
| 평소에는 입력칸이 없고 수정을 눌러야 나온다 | 읽다가 실수로 고친다 |
| 수정 화면에서 이름·순서·켜짐·새 소분류를 한 번에 저장한다 | 줄마다 저장을 눌러야 한다 |
| 이름을 바꾸면 이미 분류된 공고의 값도 바뀐다 | 공고가 목록 밖 이름을 갖는다 |
| 겹친 이름이면 아무것도 저장하지 않고 고친 값을 남긴다 | 절반만 저장되거나 고친 것을 잃는다 |
| 메모 칸과 지우기 단추는 없다 | AI 에게 가지 않는 값을 적게 된다 |
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import db, taxonomy
from app.api.settings import get_connection
from app.api.ui import SETTINGS_NAV
from app.main import app
from app.normalize.engine import insert_normalized

SEED = pathlib.Path(__file__).parent.parent / "seeds" / "job-taxonomy-zighang-20260828.json"


def add_classified_job(
    conn: sqlite3.Connection, seq: int, *, job_field: str | None, job_role: str | None
) -> None:
    """공고 한 건을 정규화까지 넣고 분류 결과를 얹는다.

    분류 호출을 실제로 돌리지 않는다 — 이 화면이 보는 것은 `normalized_jobs` 에 이미 앉은
    값이지, 그 값을 만드는 과정이 아니다.
    """
    record = {"title": f"공고 {seq}", "body": "본문", "company_name": "테스트회사"}
    cursor = conn.execute(
        """
        INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash)
        VALUES (1, ?, ?, ?)
        """,
        (f"https://x/{seq}", json.dumps(record, ensure_ascii=False), f"hash-{seq}"),
    )
    raw_id = int(cursor.lastrowid or 0)
    normalized_id = insert_normalized(conn, raw_id, [])
    conn.execute(
        "UPDATE normalized_jobs SET job_field = ?, job_role = ? WHERE id = ?",
        (job_field, job_role, normalized_id),
    )


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (id, name, list_url, status)"
        " VALUES (1, '테스트', 'https://x', 'draft')"
    )
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, '테스트')")
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


def test_네비게이션에_직무_분류가_있다() -> None:
    assert ("/taxonomy", "직무 분류") in SETTINGS_NAV


def test_화면이_열리고_네비게이션이_켜진다(client: TestClient) -> None:
    response = client.get("/taxonomy")

    assert response.status_code == 200
    assert '<a href="/taxonomy" aria-current="page"' in response.text


def seed_two(conn: sqlite3.Connection) -> tuple[int, int, int]:
    major = taxonomy.create(conn, parent_id=None, name="IT·개발", sort_order=0)
    backend = taxonomy.create(conn, parent_id=major.id, name="서버·백엔드", sort_order=0)
    front = taxonomy.create(conn, parent_id=major.id, name="프론트엔드", sort_order=1)
    other = taxonomy.create(conn, parent_id=None, name="AI·데이터", sort_order=1)
    taxonomy.create(conn, parent_id=other.id, name="데이터분석")
    return major.id, backend.id, front.id


def test_표가_비어있으면_기본_분류_불러오기가_보인다(client: TestClient) -> None:
    body = client.get("/ui/taxonomy").text

    assert "기본 분류 불러오기" in body


def test_대분류를_고르면_그_소분류가_칩으로_보인다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    seed_two(conn)
    other = taxonomy.list_majors(conn)[1]

    first = client.get("/ui/taxonomy").text
    chosen = client.get(f"/ui/taxonomy?major={other.id}").text

    assert first.count("taxonomy-major") == 2
    assert "서버·백엔드" in first and "데이터분석" not in first
    assert "데이터분석" in chosen and "서버·백엔드" not in chosen
    assert "기본 분류 불러오기" not in first


def test_평소에는_입력칸이_없고_수정을_눌러야_나온다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    major_id, _, _ = seed_two(conn)

    view = client.get(f"/ui/taxonomy?major={major_id}").text
    edit = client.get(f"/ui/taxonomy?major={major_id}&edit=true").text

    assert 'name="minor_name"' not in view
    assert ">수정</button>" in view
    assert edit.count('name="minor_name"') == 3  # 두 줄과 새 줄 틀
    assert "저장" in edit
    assert "메모" not in view + edit
    assert "삭제" not in view + edit and "지우기" not in view + edit


def save(
    client: TestClient, major_id: int, name: str, enabled: bool, rows: list[tuple[str, str, bool]]
):  # type: ignore[no-untyped-def]
    data: dict[str, object] = {
        "name": name,
        "enabled": "1" if enabled else "0",
        "minor_id": [row[0] for row in rows],
        "minor_name": [row[1] for row in rows],
        "minor_on": ["1" if row[2] else "0" for row in rows],
    }
    return client.put(f"/ui/taxonomy/{major_id}", data=data)


def test_이름_순서_켜짐_새_소분류를_한_번에_저장한다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    major_id, backend, front = seed_two(conn)

    body = save(
        client,
        major_id,
        "IT·개발",
        True,
        [(str(front), "프론트엔드", False), (str(backend), "백엔드", True), ("", "DBA", True)],
    ).text

    assert "저장했습니다" in body
    minors = taxonomy.list_minors(conn, major_id)
    assert [(m.name, m.enabled) for m in minors] == [
        ("프론트엔드", False),
        ("백엔드", True),
        ("DBA", True),
    ]


def test_이름을_바꾸면_이미_분류된_공고의_값도_바뀐다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    major_id, backend, front = seed_two(conn)
    add_classified_job(conn, 1, job_field="IT·개발", job_role="서버·백엔드")
    add_classified_job(conn, 2, job_field="AI·데이터", job_role="서버·백엔드")

    body = save(
        client,
        major_id,
        "개발",
        True,
        [(str(backend), "백엔드", True), (str(front), "프론트엔드", True)],
    ).text

    assert "IT·개발 → 개발" in body and "서버·백엔드 → 백엔드" in body
    rows = conn.execute("SELECT job_field, job_role FROM normalized_jobs ORDER BY id").fetchall()
    assert [(r["job_field"], r["job_role"]) for r in rows] == [
        ("개발", "백엔드"),
        ("AI·데이터", "서버·백엔드"),
    ]


def test_겹친_이름이면_아무것도_저장하지_않고_고친_값을_남긴다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    major_id, backend, front = seed_two(conn)

    body = save(
        client,
        major_id,
        "개발로 바꿈",
        True,
        [(str(backend), "같은이름", True), (str(front), "같은이름", True)],
    ).text

    assert "같은 이름이 두 번 있다" in body
    assert 'value="개발로 바꿈"' in body
    assert taxonomy.list_majors(conn)[0].name == "IT·개발"
    assert [m.name for m in taxonomy.list_minors(conn, major_id)] == ["서버·백엔드", "프론트엔드"]


def test_이름을_맞바꿔도_저장된다(client: TestClient, conn: sqlite3.Connection) -> None:
    major_id, backend, front = seed_two(conn)

    save(
        client,
        major_id,
        "IT·개발",
        True,
        [(str(backend), "프론트엔드", True), (str(front), "서버·백엔드", True)],
    )

    assert [m.name for m in taxonomy.list_minors(conn, major_id)] == ["프론트엔드", "서버·백엔드"]


def test_대분류를_추가하면_수정_화면으로_열린다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    seed_two(conn)

    body = client.post("/ui/taxonomy/majors", data={"name": "게임"}).text

    assert [m.name for m in taxonomy.list_majors(conn)][-1] == "게임"
    assert 'value="게임"' in body and "저장" in body


def test_기본_분류_불러오기를_누르면_씨앗_전부가_들어온다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    data = json.loads(SEED.read_text(encoding="utf-8"))
    expected_majors = len(data["majors"])

    body = client.post("/ui/taxonomy/seed").text

    assert f"대분류 {expected_majors}개" in body
    assert "기본 분류 불러오기" not in body
    assert len(taxonomy.list_majors(conn)) == expected_majors
