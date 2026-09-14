"""검수 화면의 판정 칸 (2026-09-14 결정).

실사이트에 나가지 않는다. 저장된 행을 넣고 화면 경로로만 연다.

DB 에는 오공고 enum 이름(`INTERN`)으로 저장하고 화면에는 한글 이름을 보인다. 분류가 근거 문장을
찾지 못한 채 고른 값은 남기되 `근거 없음` 으로 보여 사람이 먼저 보게 한다.

| 확인 | 깨지면 |
|---|---|
| 표의 판정 칸이 한글 이름으로 나온다 | 운영자가 `EXPERIENCED` 를 읽고 고쳐야 한다 |
| 모달의 판정 칸은 한글 이름의 선택 목록이고 값은 enum 이름이다 | 목록 밖 값이 저장된다 |
| 목록 밖의 옛 값은 지우지 않고 한 줄 더 보인다 | 모달을 열고 저장만 해도 옛 값이 사라진다 |
| 근거 문장이 있으면 보이고, 없으면 `근거 없음` 이다 | 근거 없이 고른 값을 사람이 가려내지 못한다 |
| 목록에서 고른 값이 보정으로 저장된다 | 선택 목록이 저장 경로와 어긋난다 |
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
from app.classify.store import save_classification
from app.main import app

LIST_URL = "https://example.test/jobs/"
TITLE_EVIDENCE = "[채용연계형 인턴]"


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES ('예시', ?, 'promoted')",
        (LIST_URL,),
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '예시 채용')")
    connection.execute(
        """
        INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)
        VALUES (7, 1, ?, '{}', 'hash-7')
        """,
        (LIST_URL,),
    )
    connection.execute(
        """
        INSERT INTO normalized_jobs (id, raw_job_id, company_name, title, source_url,
                                     employment_type, experience_type, education_level)
        VALUES (3, 7, '예시', '영업 인턴', ?, 'INTERN', 'EXPERIENCED', 'Permanent')
        """,
        (LIST_URL,),
    )
    # 고용 형태만 근거 문장을 찾았다. 경력 구분·학력은 근거 없이 고른 값이다
    save_classification(
        connection,
        7,
        {"employment_type": "INTERN", "experience_type": "EXPERIENCED"},
        model="m",
        evidence={"employment_type": TITLE_EVIDENCE},
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


def _cell(html: str, field: str) -> str:
    found = re.search(
        rf'id="review-cell-3-{field}"[^>]*>\s*<span[^>]*>([^<]*)</span>', html, re.DOTALL
    )
    assert found is not None, f"{field} 칸이 표에 없다"
    return found.group(1)


def _select(html: str, field: str) -> str:
    found = re.search(rf'<select id="review-field-{field}".*?</select>', html, re.DOTALL)
    assert found is not None, f"{field} 선택 목록이 모달에 없다"
    return found.group(0)


def test_표의_판정_칸은_한글_이름이다(client: TestClient) -> None:
    html = client.get("/ui/review").text

    assert _cell(html, "employment_type") == "인턴"
    assert _cell(html, "experience_type") == "경력"
    # 목록 밖의 옛 값은 이름이 없어 그대로 보인다
    assert _cell(html, "education_level") == "Permanent"


def test_모달의_판정_칸은_한글_이름의_선택_목록이다(client: TestClient) -> None:
    html = client.get("/ui/review/modal/3").text

    employment = _select(html, "employment_type")
    assert '<option value="INTERN" selected>인턴</option>' in employment
    assert '<option value="PART_TIME">파트타임</option>' in employment
    assert '<option value="">값 없음</option>' in employment
    assert '<option value="true">채용 시 마감</option>' in _select(html, "closes_when_filled")
    assert '<option value="Permanent" selected>Permanent (목록 밖)</option>' in _select(
        html, "education_level"
    )


def test_근거가_있으면_보이고_없으면_근거_없음이다(client: TestClient) -> None:
    html = client.get("/ui/review/modal/3").text

    assert f"근거: {TITLE_EVIDENCE}" in html
    # 값이 있는데 근거가 없는 칸은 경력 구분과 학력 둘이다. 값이 없는 칸에는 붙지 않는다
    assert html.count(">근거 없음</span>") == 2


def test_목록에서_고른_값이_보정으로_저장된다(client: TestClient, conn: sqlite3.Connection) -> None:
    client.put("/ui/review/jobs/3", data={"employment_type": "PART_TIME"})

    row = conn.execute(
        "SELECT value FROM job_field_overrides"
        " WHERE raw_job_id = 7 AND field_name = 'employment_type'"
    ).fetchone()
    assert row["value"] == "PART_TIME"
