"""직접 넣은 공고의 원문을 다시 넣어 바꾸고 곧바로 다시 분류하는 것 (`app/crawler/manual.py`의
`replace_source`, 공고 패널의 `원문 다시 넣기`).

실사이트도 모델도 부르지 않는다. 이미지 읽기와 분류는 정해 둔 답을 돌려주는 대역이다.

| 확인 | 깨지면 |
|---|---|
| 새 이미지·글로 원문을 갈아 끼우고 지금 값은 이력에 남긴다 | 잘못 넣은 원문을 되돌릴 길이 없다 |
| 같은 행을 쓴다 | 공고 번호·사람 보정·전달 표시가 떨어져 나간다 |
| 사이트 공고는 바꾸지 않는다 | 원문 다시 수집이 사람이 넣은 원문을 덮는다 |
| 읽을 것이 없으면 아무것도 바꾸지 않는다 | 이미지를 못 읽었는데 원문이 비워진다 |
| 패널의 단추 하나로 바꾸고 다시 분류한다 | 원문을 바꾼 뒤 다시 채우기를 따로 눌러야 한다 |
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import crawlers as crawlers_api
from app.api import review_actions
from app.classify.batch import ClassifyProgress
from app.crawler.images import IMAGE_TEXT_MARK, ImageReadError
from app.crawler.manual import ManualAddError, add_posting, replace_source
from app.llm.base import ImageInput
from app.main import app
from tests.test_manual_posting import URL, FakeReader, _png, blocked

OLD = "[예시] 마케터 채용\n\n주요업무\n- 광고 운영"


class FailingReader:
    async def read(self, parts: Sequence[ImageInput]) -> str:
        raise ImageReadError("이미지 읽기 응답을 읽을 수 없다")


@pytest.fixture
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "jobs.db"


@pytest.fixture
def conn(db_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(db_path)
    db.migrate_up(connection)
    try:
        yield connection
    finally:
        connection.close()


async def _added(conn: sqlite3.Connection) -> int:
    fetcher = blocked()
    try:
        added = await add_posting(conn, URL, company="예시회사", fetcher=fetcher, text=OLD)
    finally:
        await fetcher.aclose()
    conn.commit()
    return added.raw_job_id


def _record(conn: sqlite3.Connection, raw_job_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT raw_data_json FROM raw_jobs WHERE id = ?", (raw_job_id,)).fetchone()
    return dict(json.loads(row["raw_data_json"]))


@pytest.mark.asyncio
async def test_이미지로_원문을_바꾸고_지금_값은_이력에_남긴다(conn: sqlite3.Connection) -> None:
    raw_job_id = await _added(conn)

    notes = await replace_source(conn, raw_job_id, images=[_png()], reader=FakeReader())

    record = _record(conn, raw_job_id)
    assert record["body"] == f"{IMAGE_TEXT_MARK}\n모집분야: 설계\n지원자격: 학사 이상"
    # 회사 이름은 처음 넣은 값을 지킨다
    assert record["company_name"] == "예시회사"
    history = conn.execute(
        "SELECT raw_data_json, run_id FROM raw_job_history WHERE raw_job_id = ?", (raw_job_id,)
    ).fetchall()
    assert len(history) == 1
    assert json.loads(history[0]["raw_data_json"])["body"] == OLD
    assert history[0]["run_id"] is None
    # 같은 행이다. 새 수집 건이 생기지 않는다
    assert conn.execute("SELECT count(*) FROM raw_jobs").fetchone()[0] == 1
    body = conn.execute(
        "SELECT body FROM normalized_jobs WHERE raw_job_id = ?", (raw_job_id,)
    ).fetchone()["body"]
    assert "지원자격: 학사 이상" in body
    assert "올린 이미지 1장에서 글을 읽어 원문에 붙였다" in notes


@pytest.mark.asyncio
async def test_읽을_것이_없으면_아무것도_바꾸지_않는다(conn: sqlite3.Connection) -> None:
    raw_job_id = await _added(conn)

    with pytest.raises(ManualAddError, match="바꿀 글이나 이미지"):
        await replace_source(conn, raw_job_id)
    with pytest.raises(ManualAddError, match="올린 이미지를 읽지 못했다"):
        await replace_source(conn, raw_job_id, images=[_png()], reader=FailingReader())

    assert _record(conn, raw_job_id)["body"] == OLD
    assert conn.execute("SELECT count(*) FROM raw_job_history").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_사이트_공고는_원문을_바꾸지_않는다(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO crawlers (name, list_url, status)"
        " VALUES ('사이트', 'https://a.test/', 'promoted')"
    )
    conn.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '사이트')")
    conn.execute(
        "INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)"
        " VALUES (99, 1, 'https://a.test/1', '{}', 'h')"
    )
    conn.commit()

    with pytest.raises(ManualAddError, match="직접 넣은 공고만"):
        await replace_source(conn, 99, text="새 글")


@pytest.fixture
def client(db_path: pathlib.Path, conn: sqlite3.Connection) -> Iterator[TestClient]:
    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(db_path)
        try:
            yield connection
        finally:
            connection.close()

    app.dependency_overrides[crawlers_api.get_connection] = request_connection
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _normalized_id(conn: sqlite3.Connection, raw_job_id: int) -> int:
    return int(
        conn.execute(
            "SELECT id FROM normalized_jobs WHERE raw_job_id = ?", (raw_job_id,)
        ).fetchone()["id"]
    )


@pytest.mark.asyncio
async def test_패널의_단추_하나로_원문을_바꾸고_다시_분류한다(
    client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_job_id = await _added(conn)
    normalized_id = _normalized_id(conn, raw_job_id)
    classified: list[list[int]] = []

    async def classify(
        connection: sqlite3.Connection, ids: list[int], progress: ClassifyProgress, **_: Any
    ) -> ClassifyProgress:
        classified.append(ids)
        progress.processed = 1
        return progress

    monkeypatch.setattr(review_actions, "classify_ids", classify)
    monkeypatch.setattr(review_actions, "LlmImageReader", lambda _conn: FakeReader())

    panel = client.get(f"/ui/review/jobs/{normalized_id}/panel").text
    assert "원문 다시 넣기" in panel
    assert f'hx-post="/ui/review/jobs/{normalized_id}/replace-source"' in panel
    # 패널 전체가 고치기 폼이다. 폼 안의 폼은 브라우저가 버리므로 바꾸기 폼은 패널 폼이 닫힌 뒤에 있다
    assert panel.index('<form id="job-replace-form"') > panel.index("</form>")
    assert panel.count("<form") == 2

    html = client.post(
        f"/ui/review/jobs/{normalized_id}/replace-source",
        data={"text": ""},
        files=[("images", ("posting.png", _png(), "image/png"))],
    ).text

    assert "원문을 바꾸고 AI로 다시 채웠어요" in html
    assert classified == [[raw_job_id]]
    assert "지원자격: 학사 이상" in _record(conn, raw_job_id)["body"]


def test_사이트_공고_패널에는_원문_다시_넣기가_없다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    conn.execute(
        "INSERT INTO crawlers (name, list_url, status)"
        " VALUES ('사이트', 'https://a.test/', 'promoted')"
    )
    conn.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '사이트')")
    conn.execute(
        "INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)"
        " VALUES (1, 1, 'https://a.test/1', '{}', 'h')"
    )
    conn.execute(
        "INSERT INTO normalized_jobs (raw_job_id, title, source_url) VALUES (1, '공고', 'https://a.test/1')"
    )
    conn.commit()

    assert "원문 다시 넣기" not in client.get("/ui/review/jobs/1/panel").text
