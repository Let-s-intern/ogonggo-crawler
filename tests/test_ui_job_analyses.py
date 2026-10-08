"""공고 분석 화면 (2026-10-08, LC-3446).

AI 와 오공고 서버를 부르지 않는다 (`app/api/ui_job_analyses.py`).

| 확인 | 깨지면 |
|---|---|
| 위 메뉴에 켜지고 기록이 상태·출처와 함께 나온다 | 무엇을 분석해 보냈는지 볼 곳이 없다 |
| 상태·출처·검색으로 거른다 | 실패한 공고만 골라 볼 수 없다 |
| 행을 열면 오공고 화면 모양의 분석과 실패 사유가 나온다 | 무엇이 나갔는지 JSON 으로만 본다 |
| 다시 보내기는 AI 없이 남은 분석을 보낸다 | 전송만 실패한 공고에 토큰을 다시 쓴다 |
| 멈춤 시각이 시작보다 이르면 저장하지 않는다 | 매일 분석이 시작하자마자 멈춘다 |
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db
from app.api.settings import get_connection
from app.config import Settings, get_settings
from app.deliver import settings as deliver_store
from app.job_analysis import ogonggo, store
from app.main import app
from tests.test_job_analysis import answer
from tests.test_job_analysis_runner import FakeOgonggo


def analyzed() -> store.Analyzed:
    body = answer()
    data = {
        "tasks": body["tasks"],
        "required": body["required"],
        "preferred": [],
        "employment": {k: {"value": None, "note": None} for k in body["employment"]},
        "submission": {k: {"value": None, "note": None} for k in body["submission"]},
        "competencies": [{**body["competencies"][0], "experiences": ["a 한 경험"]}],
    }
    return store.Analyzed(data, 2, "fake-model", 1000, 500)


@pytest.fixture
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "jobs.db"
    connection = db.connect(path)
    db.migrate_up(connection)
    deliver_store.write_config(connection, deliver_store.DeliverConfig(url="https://ogonggo.test"))
    for job_id, source, title in [(1, "WORK24", "물류 사무보조"), (2, "CRAWLER", "PO 인턴")]:
        store.save_target(
            connection,
            job_id,
            source,
            {"title": title, "companyName": "회사", "qualifications": "x"},
            "h",
        )
        store.save_analysis(connection, job_id, analyzed(), None)
    store.mark(connection, 1, store.SENT)
    store.mark(connection, 2, store.UNSENT, "오공고가 받지 않았다(500)")
    connection.close()
    return path


@pytest.fixture
def client(db_path: pathlib.Path) -> Iterator[TestClient]:
    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(db_path)
        try:
            yield connection
        finally:
            connection.close()

    app.dependency_overrides[get_connection] = request_connection
    app.dependency_overrides[get_settings] = lambda: Settings(ogonggo_internal_api_key="key")
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def conn(db_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(db_path)
    try:
        yield connection
    finally:
        connection.close()


def test_위_메뉴에_켜지고_기록이_나온다(client: TestClient) -> None:
    page = client.get("/job-analyses").text
    panel = client.get("/ui/job-analyses").text

    assert 'aria-current="page"' in page and "공고 분석" in page
    assert "물류 사무보조" in panel and "PO 인턴" in panel
    assert "고용24" in panel and "크롤러" in panel
    assert "오공고가 받지 않았다(500)" in panel


@pytest.mark.parametrize(
    ("query", "shown", "hidden"),
    [
        ("status=unsent", "PO 인턴", "물류 사무보조"),
        ("source=WORK24", "물류 사무보조", "PO 인턴"),
        ("q=물류", "물류 사무보조", "PO 인턴"),
    ],
)
def test_상태_출처_검색으로_거른다(client: TestClient, query: str, shown: str, hidden: str) -> None:
    panel = client.get(f"/ui/job-analyses?{query}").text

    assert shown in panel and hidden not in panel


def test_행을_열면_오공고_화면_모양의_분석이_나온다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    record = store.get(conn, 2)
    assert record is not None

    html = client.get(f"/ui/job-analyses/{record['id']}").text

    assert "오공고 체크포인트" in html and "공고 속 문구" in html
    assert "못 보냄" in html and "오공고가 받지 않았다(500)" in html
    assert "판 2" in html


def test_다시_보내기는_AI_없이_남은_분석을_보낸다(
    client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FakeOgonggo([])
    monkeypatch.setattr(ogonggo, "transport", httpx.MockTransport(server.handler))
    record = store.get(conn, 2)
    assert record is not None

    html = client.post(f"/ui/job-analyses/{record['id']}/resend").text

    assert "오공고로 보냈어요" in html
    assert server.puts[0][0] == 2
    assert server.puts[0][1]["guideVersion"] == 2
    assert json.loads(json.dumps(server.puts[0][1]["analysis"]))["tasks"][0]["tag"] == "기획"
    assert store.get(conn, 2)["status"] == "sent"  # type: ignore[index]


def test_멈춤_시각이_시작보다_이르면_저장하지_않는다(client: TestClient) -> None:
    html = client.put(
        "/ui/job-analyses/settings", data={"start_time": "11:10", "stop_time": "11:00"}
    ).text

    assert "멈춤 시각은 시작 시각보다 늦어야 한다" in html
