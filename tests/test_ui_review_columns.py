"""검수 표의 열 수와 빈 표의 colspan (7.1.V).

실사이트에 나가지 않는다. 저장된 행을 넣고 화면 경로로만 연다.

열이 바뀔 때마다 사람이 두 자리를 고쳐야 한다 — 머리글과 `empty_row` 의 colspan 이다. 한쪽만
고치면 화면은 조용히 어긋난다. 행이 있는 동안은 아무 표시도 나지 않고, 조건이 0건이 된 날에야
안내 문구가 왼쪽 몇 칸에 갇힌 채 나온다. 그 모습은 "조건에 맞는 것이 없다" 가 아니라 표가
깨진 것으로 읽힌다.

그래서 이 파일은 colspan 을 상수로 적지 않는다. 머리글에서 열을 세고 그 수와 같은지만 본다.
상수를 적으면 열이 바뀌는 날 이 파일도 같이 틀린다.

| 확인 | 깨지면 |
|---|---|
| 빈 표의 colspan 이 머리글 열 수와 같다 | 0건 안내가 칸 하나에 갇힌다 |
| 중복 조건이 걸리면 두 수가 함께 하나씩 는다 | 묶음 열을 더한 쪽만 어긋난다 |
| 값 칸이 `OVERRIDABLE_FIELDS` 와 같은 수다 | 지운 칸의 머리글이 남거나 새 칸이 빠진다 |
| 0016 이 지운 칸 중 되살리지 않은 둘의 이름표가 없다 | 저장할 수 없는 칸을 운영자가 고치려 든다 |
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

PARENT = "엘지"

# 검수 표 하나만 본다. 화면에는 빈 값 건수 표와 중복 묶음 표도 있어서 문서 전체에서 세면
# 그 표들의 머리글까지 섞인다
REVIEW_TABLE = re.compile(r"<caption>검수 대상 공고</caption>.*?</table>", re.DOTALL)
HEAD_CELL = re.compile(r'<th scope="col"')
EMPTY_COLSPAN = re.compile(r'<tr><td colspan="(\d+)"')

# 머리글에서 값 칸을 뺀 나머지. 고르기·번호·고치기·전달·보정·모회사와
# 워크플로우·수집 시각·원문이다
FIXED_COLUMNS = 9

# 0016 이 `normalized_jobs` 에서 지운 칸의 이름표. 표에 남아 있으면 안 된다.
# 모집인원은 0028 이 `recruitment_headcount` 로 다시 더해 여기서 뺐다 — 옛 `headcount` 가
# 아니라 분류가 채우는 새 칸이다 (`migrations/0028_add_posting_detail_fields.sql`)
DROPPED_LABELS = ("부서", "직군")


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        """
        INSERT INTO crawlers (id, name, list_url, status, default_company)
        VALUES (1, '엘지 채용', 'https://lg.example.test/', 'promoted', ?)
        """,
        (PARENT,),
    )
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, '엘지 채용')")
    for raw_id in (1, 2):
        source_url = f"https://lg.example.test/{raw_id}"
        connection.execute(
            """
            INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)
            VALUES (?, 1, ?, '{}', ?)
            """,
            (raw_id, source_url, f"hash-{raw_id}"),
        )
        connection.execute(
            """
            INSERT INTO normalized_jobs
                   (raw_job_id, parent_company, company, title, body, source_url)
            VALUES (?, ?, '엘지전자', ?, '본문', ?)
            """,
            (raw_id, PARENT, f"공고 {raw_id}", source_url),
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


def _table(client: TestClient, params: dict[str, str]) -> str:
    """검수 표 하나만 잘라 낸다."""
    found = REVIEW_TABLE.search(client.get("/ui/review", params=params).text)
    assert found is not None, "검수 표가 화면에 없다"
    return found.group(0)


def _head_count(client: TestClient, params: dict[str, str]) -> int:
    return len(HEAD_CELL.findall(_table(client, params)))


def _empty_colspan(client: TestClient, params: dict[str, str]) -> int:
    """0건인 조건으로 열어 안내 행의 colspan 을 읽는다."""
    found = EMPTY_COLSPAN.search(_table(client, {**params, "q": "이런제목은없다"}))
    assert found is not None, "0건 안내 행이 없다"
    return int(found.group(1))


def test_빈_표의_colspan_이_머리글_열_수와_같다(client: TestClient) -> None:
    assert _empty_colspan(client, {}) == _head_count(client, {})


def test_중복_조건이_걸려도_두_수가_같다(client: TestClient) -> None:
    """묶음 열이 하나 늘고, 안내 행도 같이 하나 늘어야 한다."""
    dup = {"dup": "title"}

    assert _head_count(client, dup) == _head_count(client, {}) + 1
    assert _empty_colspan(client, dup) == _head_count(client, dup)


def test_값_칸의_수가_고칠_수_있는_필드와_같다(client: TestClient) -> None:
    assert _head_count(client, {}) == FIXED_COLUMNS + len(OVERRIDABLE_FIELDS)


def test_지운_세_칸의_이름표가_표에_없다(client: TestClient) -> None:
    table = _table(client, {})

    for label in DROPPED_LABELS:
        assert f">{label}</th>" not in table
