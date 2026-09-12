"""검수 편집 모달 (16.1, 18.4).

실사이트에 나가지 않는다. 저장된 행을 넣고 화면 경로로만 고친다.

한 건을 통째로 여는 모달이다. 필드 하나만 여는 경로는 남기지 않는다 — 값 하나만 보고는 그
값이 맞는지 판정할 수 없어서 같은 건을 여섯 번 열게 된다.

| 확인 | 깨지면 |
|---|---|
| 표는 읽기만 하고 행의 `수정` 하나가 모달을 연다 | 입구가 둘이면 어느 쪽이 저장된 값인지 모른다 |
| 모달에 여섯 필드가 다 들어오고 규칙값을 함께 적는다 | 다른 필드가 맞는지 보려고 하나씩 열게 된다 |
| 두 필드를 고쳐 한 번에 저장하면 보정이 둘 쌓인다 | 여러 곳을 고치려면 저장을 여섯 번 누른다 |
| 손대지 않은 필드에는 보정을 만들지 않는다 | 한 곳을 고쳤는데 여섯이 전부 사람 보정으로 굳는다 |
| 줄바꿈만 다른 값(CRLF)은 고친 것이 아니다 | 저장할 때마다 본문·자격요건에 빈 보정이 생긴다 |
| 저장 응답이 표의 그 행만 OOB 로 함께 갈아 끼운다 | 모달을 닫았더니 표에 옛 값이 남는다 |
| 저장 응답이 모달을 닫으라고 알린다 | 저장했는데 모달이 그대로 열려 있다 |
| 보정 삭제는 필드마다 모달 안에서 되고 모달을 닫지 않는다 | 되돌리려고 모달을 다시 열어야 한다 |
| 전달된 행이면 모달 안에서 알린다 | 소비 측에 반영되지 않는 수정을 반영된 줄 알고 한다 |
| 고치는 모달 안에는 지우는 경로가 없다 | 입력 칸 옆에서 고치려다 지운다 |
| `delivered_at` 과 `normalized_jobs` 가 그대로다 | 보낸 공고가 다시 가고, 파생값에 손으로 쓴다 |
"""

from __future__ import annotations

import pathlib
import re
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import crawlers as crawlers_api
from app.main import app
from app.normalize.engine import OVERRIDABLE_FIELDS

LIST_URL = "https://www.python.org/jobs/"
LONG_BODY = "본문 첫 줄\n" + ("이 자리는 표 칸 폭에 들어가지 않는 긴 본문이다. " * 20)

# 모달이 돌려보내는 여섯 칸 전부. 브라우저는 손대지 않은 칸도 같이 보낸다
FULL_FORM = {
    "company": "파이썬재단",
    "title": "백엔드 개발자",
    "job_role": "",
    "work_location": "",
    "deadline": "",
    "body": LONG_BODY,
    "requirements": "",
}


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES ('python.org', ?, 'promoted')",
        (LIST_URL,),
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, 'python.org 채용')")
    connection.execute(
        """
        INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)
        VALUES (7, 1, ?, '{}', 'hash-7')
        """,
        (LIST_URL,),
    )
    connection.execute(
        """
        INSERT INTO normalized_jobs (id, raw_job_id, company, title, body, source_url)
        VALUES (3, 7, '파이썬재단', '백엔드 개발자', ?, ?)
        """,
        (LONG_BODY, LIST_URL),
    )
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

    app.dependency_overrides[crawlers_api.get_connection] = request_connection
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def override_of(conn: sqlite3.Connection, field: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM job_field_overrides WHERE raw_job_id = 7 AND field_name = ?",
        (field,),
    ).fetchone()
    return None if row is None else str(row["value"])


def override_fields(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT field_name FROM job_field_overrides WHERE raw_job_id = 7 ORDER BY field_name"
    ).fetchall()
    return [str(row["field_name"]) for row in rows]


def normalized_row(conn: sqlite3.Connection) -> dict[str, object]:
    row = conn.execute("SELECT * FROM normalized_jobs WHERE raw_job_id = 7").fetchone()
    return dict(row)


def oob_ids(html: str) -> list[str]:
    """응답에 실려 표의 제자리로 들어가는 조각들. 표 전체가 오면 이 목록이 비어 있다."""
    return re.findall(r'<div id="([^"]+)"[^>]* hx-swap-oob="true"', html)


def test_표는_읽기만_하고_행의_수정_버튼이_모달을_연다(client: TestClient) -> None:
    """표 안에서 바로 고치는 입력은 남기지 않는다. 고치는 자리는 모달 하나다."""
    html = client.get("/ui/review").text

    assert 'id="review-open-3"' in html
    assert "data-modal-open" in html
    assert 'hx-get="/ui/review/modal/3"' in html
    # 필드 하나만 여는 입구는 없다
    assert "/ui/review/modal/3/body" not in html
    assert "<textarea" not in html  # 표 안에서 바로 고치는 입력이 없다
    assert "hx-put=" not in html


def test_모달에_여섯_필드가_다_들어오고_규칙값을_함께_보여준다(client: TestClient) -> None:
    html = client.get("/ui/review/modal/3").text

    for name in ("company", "title", "work_location", "deadline", "body", "requirements"):
        assert f'name="{name}"' in html, name
    assert "규칙이 만든 값" in html
    assert "본문 첫 줄" in html
    assert "<textarea" in html  # 본문과 자격요건은 여러 줄 입력이다
    assert 'hx-put="/ui/review/jobs/3"' in html
    # 고치지 않는 값도 조회 상세처럼 함께 보인다
    assert "수집 시각" in html
    assert "정규화 시각" in html


def test_검수_표와_모달에_직무_열이_나온다(client: TestClient) -> None:
    """0017 이 더한 칸이다. 화면에 없으면 잘못 뽑힌 직무를 아무도 보지 못한다 (2.5.V)."""
    table = client.get("/ui/review").text
    modal = client.get("/ui/review/modal/3").text

    assert "직무" in table
    assert 'id="review-cell-3-job_role"' in table
    assert 'name="job_role"' in modal


def test_보정된_필드는_규칙이_만든_값을_함께_보여준다(client: TestClient) -> None:
    """무엇에서 고친 것인지 모르면 그 보정이 맞는지 판정할 수 없다."""
    client.put("/ui/review/jobs/3", data={**FULL_FORM, "company": "파이썬 소프트웨어 재단"})

    html = client.get("/ui/review/modal/3").text

    assert "규칙이 만든 값" in html
    assert "파이썬재단" in html  # 규칙이 만든 값이 그대로 남아 있다
    assert "파이썬 소프트웨어 재단" in html  # 고칠 칸에는 사람이 정한 값이 들어 있다


def test_두_필드를_한_번에_저장하면_보정이_둘_쌓인다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    before = normalized_row(conn)

    response = client.put(
        "/ui/review/jobs/3",
        data={**FULL_FORM, "company": "파이썬 소프트웨어 재단", "work_location": "판교"},
    )

    assert response.status_code == 200
    assert override_fields(conn) == ["company", "work_location"]
    assert override_of(conn, "company") == "파이썬 소프트웨어 재단"
    assert override_of(conn, "work_location") == "판교"
    # 손대지 않은 칸에는 보정이 생기지 않는다
    assert override_of(conn, "title") is None
    assert override_of(conn, "body") is None
    # 파생값에는 손으로 쓰지 않는다
    assert normalized_row(conn) == before
    # 표의 값 칸 전부와 보정 개수·전달 칸만 갈린다. 표 전체는 다시 그리지 않는다.
    # 칸 목록을 여기 베껴 적지 않는다 — 0012 가 넓혔고 0016 이 셋을 지웠다
    assert oob_ids(response.text) == [
        *(f"review-cell-3-{field}" for field in OVERRIDABLE_FIELDS),
        "review-override-count-3",
        "review-delivery-3",
    ]
    # 표 조각 자체는 오지 않는다. 모달 안의 표(고치지 않는 값)와 구분해 캡션으로 본다
    assert "검수 대상 공고" not in response.text
    # 표가 갈린 뒤에 닫는다. 먼저 닫으면 옛 값이 남은 표가 드러난다
    assert response.headers["HX-Trigger-After-Settle"] == "app-modal-done"


def test_고친_값이_없으면_보정을_만들지_않고_닫지도_않는다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """조용히 닫으면 저장된 줄 알고, 아무 데도 남지 않은 수정을 나중에 찾게 된다."""
    response = client.put("/ui/review/jobs/3", data=FULL_FORM)

    assert override_fields(conn) == []
    assert "고친 값이 없다" in response.text
    assert "HX-Trigger-After-Settle" not in response.headers
    assert oob_ids(response.text) == []


def test_줄바꿈만_다른_본문은_고친_것으로_보지_않는다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """브라우저는 textarea 를 CRLF 로 보낸다. 그것을 변경으로 읽으면 저장할 때마다 보정이 는다."""
    crlf = LONG_BODY.replace("\n", "\r\n")

    client.put("/ui/review/jobs/3", data={**FULL_FORM, "body": crlf})

    assert override_fields(conn) == []


def test_빈_값으로_고치면_그것도_보정이다(client: TestClient, conn: sqlite3.Connection) -> None:
    """빈 문자열은 "이 필드는 비어 있는 것이 맞다" 는 판단이다. 보정이 없는 것과 다르다."""
    client.put("/ui/review/jobs/3", data={**FULL_FORM, "title": ""})

    assert override_fields(conn) == ["title"]
    assert override_of(conn, "title") == ""


def test_모달_안에서_필드마다_보정을_지우고_모달은_열려_있다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    client.put(
        "/ui/review/jobs/3",
        data={**FULL_FORM, "title": "사람이 고친 제목", "work_location": "판교"},
    )
    assert override_fields(conn) == ["title", "work_location"]

    response = client.put(
        "/ui/review/jobs/3",
        data={**FULL_FORM, "title": "사람이 고친 제목", "work_location": "판교", "drop": "title"},
    )

    assert override_fields(conn) == ["work_location"]
    assert "제목 보정을 지웠다" in response.text
    # 되돌리고 나머지를 계속 본다. 닫지 않는다
    assert "HX-Trigger-After-Settle" not in response.headers
    assert "review-cell-3-title" in oob_ids(response.text)
    # 아직 저장하지 않은 다른 칸의 값은 그대로 남는다
    assert "판교" in response.text


def test_전달된_행은_모달_안에서_알리고_전달_표시는_그대로다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    conn.execute("UPDATE normalized_jobs SET delivered_at = '2020-01-01 00:00:00'")

    opened = client.get("/ui/review/modal/3").text
    assert "이미 전달된 행이다" in opened

    client.put("/ui/review/jobs/3", data={**FULL_FORM, "title": "전달 뒤에 고친 제목"})

    row = conn.execute("SELECT delivered_at FROM normalized_jobs WHERE raw_job_id = 7").fetchone()
    assert row["delivered_at"] == "2020-01-01 00:00:00"


def test_고칠_수_없는_필드는_지우지_못하고_사유를_적는다(client: TestClient) -> None:
    html = client.put("/ui/review/jobs/3", data={**FULL_FORM, "drop": "source_url"}).text

    assert "고칠 수 없는 필드다" in html
    assert oob_ids(html) == []


def test_없는_수집_건은_사유를_적는다(client: TestClient) -> None:
    html = client.get("/ui/review/modal/999").text

    assert "공고 999" in html
    assert oob_ids(html) == []


def test_고치는_모달에는_지우는_경로가_없다(client: TestClient) -> None:
    """지우기는 표에 있고, 고치는 모달 안에는 없다 (27.9, 30.2).

    Push 30 에서 조회와 검수를 한 화면으로 합쳤다. 그래서 표에는 체크박스와 지우기가 있다.
    없어야 하는 것은 값을 고치는 자리 안의 지우기다 — 입력 칸 옆에 지우기 단추가 있으면
    고치려다 지우는 사고가 그 자리에서 난다.
    """
    modal = client.get("/ui/review/modal/3").text

    assert "/ui/review/delete" not in modal
    assert "data-select-row" not in modal
    assert "all_filtered" not in modal

    # 표 쪽은 반대다. 합친 화면이라 고르기와 지우기가 함께 있다
    table = client.get("/ui/review").text
    assert 'hx-post="/ui/review/delete/confirm"' in table
    assert "data-select-row" in table


def test_고치는_흐름에_브라우저_확인_창이_끼어들지_않는다(client: TestClient) -> None:
    """보정 삭제는 되돌릴 수 없는 일이 아니다. 규칙값이 바로 아래에 그대로 적혀 있다."""
    html = client.get("/ui/review/modal/3").text

    assert "보정 삭제" in html or "규칙값" in html
    assert "hx-confirm" not in html
