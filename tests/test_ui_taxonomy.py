"""직무 분류 어드민 화면 (4.1.V ~ 4.5.V).

`app/api/ui_companies.py` 와 같은 자리다 — 목록·더하기·고치기·켜기끄기가 한 화면에 있는
CRUD 조각 라우트. 공고 수는 `normalized_jobs.job_field`/`job_role` 를 세어 얹는다.
"""

from __future__ import annotations

import html
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


def test_표가_비어있으면_기본_분류_불러오기_버튼만_보인다(client: TestClient) -> None:
    body = client.get("/ui/taxonomy").text

    assert "기본 분류 불러오기" in body
    assert "직무 분류가 아직 없다" in body
    assert "직무 분류 추가" not in body


def test_대분류와_소분류가_트리로_보인다(client: TestClient, conn: sqlite3.Connection) -> None:
    major = taxonomy.create(conn, parent_id=None, name="IT·개발", sort_order=0)
    taxonomy.create(conn, parent_id=major.id, name="서버·백엔드")
    taxonomy.create(conn, parent_id=None, name="AI·데이터", sort_order=1)
    conn.commit()

    body = client.get("/ui/taxonomy").text

    assert body.index("IT·개발") < body.index("서버·백엔드") < body.index("AI·데이터")
    assert "기본 분류 불러오기" not in body


def test_공고_수가_그_이름으로_분류된_건수와_같다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    major = taxonomy.create(conn, parent_id=None, name="IT·개발")
    taxonomy.create(conn, parent_id=major.id, name="서버·백엔드")
    add_classified_job(conn, 1, job_field="IT·개발", job_role="서버·백엔드")
    add_classified_job(conn, 2, job_field="IT·개발", job_role="서버·백엔드")
    add_classified_job(conn, 3, job_field="IT·개발", job_role=None)
    conn.commit()

    body = client.get("/ui/taxonomy").text

    assert "3건" in body  # 대분류: 소분류 유무와 무관하게 IT·개발 전부
    assert "2건" in body  # 소분류: 서버·백엔드 로 소분류까지 정해진 것만


def test_켜짐_꺼짐_상태가_보인다(client: TestClient, conn: sqlite3.Connection) -> None:
    major = taxonomy.create(conn, parent_id=None, name="IT·개발")
    taxonomy.set_enabled(conn, major.id, False)
    taxonomy.create(conn, parent_id=None, name="AI·데이터")
    conn.commit()

    body = client.get("/ui/taxonomy").text

    assert "꺼짐" in body
    assert "켜짐" in body


def test_대분류를_화면에서_더한다(client: TestClient) -> None:
    response = client.post("/ui/taxonomy", data={"name": "IT·개발", "parent_id": ""})

    assert response.status_code == 200
    body = html.unescape(response.text)
    assert "대분류 'IT·개발' 를 더했다" in body
    assert "IT·개발" in body


def test_소분류를_부모를_골라_더한다(client: TestClient, conn: sqlite3.Connection) -> None:
    major = taxonomy.create(conn, parent_id=None, name="IT·개발")
    conn.commit()

    response = client.post("/ui/taxonomy", data={"name": "서버·백엔드", "parent_id": str(major.id)})

    assert response.status_code == 200
    body = html.unescape(response.text)
    assert "소분류 '서버·백엔드' 를 더했다" in body
    assert "서버·백엔드" in body


def test_더하기_폼에서_부모로_고를_대분류가_보인다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    taxonomy.create(conn, parent_id=None, name="IT·개발")
    conn.commit()

    body = client.get("/ui/taxonomy").text

    assert '<option value="1">IT·개발</option>' in body


def test_이름_순서_메모를_고치면_목록에_반영된다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    major = taxonomy.create(conn, parent_id=None, name="IT 개발")
    conn.commit()

    response = client.put(
        f"/ui/taxonomy/{major.id}",
        data={"name": "IT·개발", "sort_order": "3", "note": "씨앗 기준"},
    )

    assert response.status_code == 200
    assert "IT·개발" in response.text
    assert "씨앗 기준" in response.text


def test_이름을_고치면_저장_전에_공고_수가_보인다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """`row.job_count` 가 이름 입력 칸 옆에 늘 붙어 있다 — 고치기 전에 보인다."""
    taxonomy.create(conn, parent_id=None, name="IT·개발")
    add_classified_job(conn, 1, job_field="IT·개발", job_role=None)
    add_classified_job(conn, 2, job_field="IT·개발", job_role=None)
    conn.commit()

    body = client.get("/ui/taxonomy").text

    assert "공고 2건이 지금 이 이름으로 분류돼 있다" in body


def test_이름을_고치면_그_건수만큼_어긋난다는_경고가_뜬다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    major = taxonomy.create(conn, parent_id=None, name="IT 개발")
    add_classified_job(conn, 1, job_field="IT 개발", job_role=None)
    add_classified_job(conn, 2, job_field="IT 개발", job_role=None)
    conn.commit()

    response = client.put(
        f"/ui/taxonomy/{major.id}", data={"name": "IT·개발", "sort_order": "0", "note": ""}
    )

    assert "'IT 개발' 으로 이미 분류된 공고 2건은 새 이름과 어긋난다" in html.unescape(
        response.text
    )


def test_이름을_안_바꾸면_건수_경고가_없다(client: TestClient, conn: sqlite3.Connection) -> None:
    major = taxonomy.create(conn, parent_id=None, name="IT·개발")
    add_classified_job(conn, 1, job_field="IT·개발", job_role=None)
    conn.commit()

    response = client.put(
        f"/ui/taxonomy/{major.id}", data={"name": "IT·개발", "sort_order": "1", "note": ""}
    )

    body = html.unescape(response.text)
    assert "어긋난다는" not in body
    assert "'IT·개발' 를 저장했다" in body


def test_같은_부모_아래_이름이_중복되면_거절_사유가_보인다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    taxonomy.create(conn, parent_id=None, name="IT·개발")
    conn.commit()

    response = client.post("/ui/taxonomy", data={"name": "IT·개발", "parent_id": ""})

    assert "저장하지 못했다" in response.text
    assert "duplicate_name" in response.text


def test_끄면_꺼짐으로_바뀐다(client: TestClient, conn: sqlite3.Connection) -> None:
    major = taxonomy.create(conn, parent_id=None, name="IT·개발")
    conn.commit()

    response = client.post(f"/ui/taxonomy/{major.id}/toggle")

    body = html.unescape(response.text)
    assert "'IT·개발' 를 껐다" in body
    assert "꺼짐" in body


def test_다시_켜면_켜짐으로_바뀐다(client: TestClient, conn: sqlite3.Connection) -> None:
    major = taxonomy.create(conn, parent_id=None, name="IT·개발")
    taxonomy.set_enabled(conn, major.id, False)
    conn.commit()

    response = client.post(f"/ui/taxonomy/{major.id}/toggle")

    body = html.unescape(response.text)
    assert "'IT·개발' 를 켰다" in body
    assert "켜짐" in body


def test_꺼도_이미_분류된_공고_수는_그대로_보인다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """지운 것이 아니므로 건수가 사라지면 안 된다."""
    major = taxonomy.create(conn, parent_id=None, name="IT·개발")
    add_classified_job(conn, 1, job_field="IT·개발", job_role=None)
    add_classified_job(conn, 2, job_field="IT·개발", job_role=None)
    conn.commit()

    response = client.post(f"/ui/taxonomy/{major.id}/toggle")

    assert "2건" in response.text


def test_지우기_단추는_화면에_없다(client: TestClient, conn: sqlite3.Connection) -> None:
    taxonomy.create(conn, parent_id=None, name="IT·개발")
    conn.commit()

    body = client.get("/ui/taxonomy").text

    assert "지우기" not in body
    assert "삭제" not in body
    assert not hasattr(taxonomy, "delete")


def test_기본_분류_불러오기를_누르면_씨앗_전부가_들어온다(client: TestClient) -> None:
    data = json.loads(SEED.read_text(encoding="utf-8"))
    expected_majors = len(data["majors"])
    expected_minors = sum(len(m["minors"]) for m in data["majors"])

    response = client.post("/ui/taxonomy/seed")

    assert response.status_code == 200
    body = html.unescape(response.text)
    assert f"대분류 {expected_majors}개" in body
    assert f"소분류 {expected_minors}개" in body
    # 행마다 자기 저장 폼을 하나씩 갖는다 — 머리글 행은 폼이 없으니 여기 안 잡힌다
    assert body.count('hx-put="/ui/taxonomy/') == expected_majors + expected_minors


def test_불러온_뒤에는_기본_분류_불러오기_버튼이_사라진다(client: TestClient) -> None:
    client.post("/ui/taxonomy/seed")

    body = client.get("/ui/taxonomy").text

    assert "기본 분류 불러오기" not in body


def test_표가_비어있지_않으면_다시_불러오지_않는다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    taxonomy.create(conn, parent_id=None, name="손으로 만든 대분류")
    conn.commit()

    response = client.post("/ui/taxonomy/seed")

    body = html.unescape(response.text)
    assert "다시 불러오지 않았다" in body
    assert "손으로 만든 대분류" in body
    assert len(taxonomy.list_majors(conn)) == 1
