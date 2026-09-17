"""공고 목록에서 고치기·오공고로 보내기·AI 로 다시 채우기 (2026-09-17, LC-3344).

오공고를 실제로 부르지 않는다. `httpx.MockTransport` 가 오공고 관리자 API 인 척 답한다. AI 도
부르지 않는다 — 키가 없는 상태에서 실패 사유가 패널에 적히는지만 본다.

| 확인 | 깨지면 |
|---|---|
| 고친 칸만 사람 보정으로 남고 다시 정규화된다 | 누르지 않은 칸까지 굳거나 고친 값이 안 보인다 |
| 고친 칸은 출처가 `직접 수정` 이다 | AI 값과 사람 값을 가를 수 없다 |
| 목록 밖 값은 받지 않는다 | 오공고가 거절할 값이 보정으로 굳는다 |
| 패널의 보내기와 표의 골라 보내기가 오공고에 등록한다 | 실패한 공고를 손으로 보낼 길이 없다 |
| 이미 보낸 공고는 다시 보내지 않는다 | 같은 공고를 두 번 등록하려 든다 |
| AI 로 다시 채우지 못하면 사유를 적는다 | 눌러도 아무 일이 없는 것처럼 보인다 |
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
from app.api import crawlers as crawlers_api
from app.config import Settings
from app.deliver import spring
from app.deliver.settings import DeliverConfig, write_config
from app.main import app

KEY = "test-internal-key"


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (id, name, list_url, default_company)"
        " VALUES (1, '예시', 'https://x', '예시그룹')"
    )
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, '예시')")
    write_config(connection, DeliverConfig(url="https://ogonggo.test/", enabled=False))
    for seq, title in ((1, "백엔드 개발자"), (2, "보낸 공고")):
        source_url = f"https://x/{seq}"
        raw = {"title": title, "company_name": "예시전자", "body": "주요 업무\n서버 개발"}
        connection.execute(
            "INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)"
            " VALUES (?, 1, ?, ?, ?)",
            (seq, source_url, json.dumps(raw, ensure_ascii=False), f"hash-{seq}"),
        )
        connection.execute(
            """
            INSERT INTO normalized_jobs (id, raw_job_id, source_url, company_name,
                                         parent_company_name, title, body, employment_type,
                                         experience_type, education_level, recruitment_type,
                                         recruitment_end_at)
            VALUES (?, ?, ?, '예시전자', '예시그룹', ?, '주요 업무 서버 개발', 'FULL_TIME',
                    'EXPERIENCED', 'BACHELOR', 'PERIOD', '2099-12-31 23:59:59')
            """,
            (seq, seq, source_url, title),
        )
        # 다시 정규화하면 판정 칸은 분류 결과에서 온다. 분류 행이 없으면 그 칸이 비어 보낼 수 없다
        connection.execute(
            "INSERT INTO job_classifications (raw_job_id, model, employment_type,"
            " experience_type, education_level) VALUES (?, 'test', 'FULL_TIME', 'EXPERIENCED',"
            " 'BACHELOR')",
            (seq,),
        )
    connection.execute(
        "INSERT INTO spring_deliveries (source_url, spring_job_id, status, sent_at)"
        " VALUES ('https://x/2', 7, 'sent', datetime('now'))"
    )
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def client(
    tmp_path: pathlib.Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(tmp_path / "jobs.db")
        try:
            yield connection
        finally:
            connection.close()

    monkeypatch.setattr(spring, "get_settings", lambda: Settings(ogonggo_internal_api_key=KEY))
    app.dependency_overrides[crawlers_api.get_connection] = request_connection
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def oggonggo(monkeypatch: pytest.MonkeyPatch, status: int = 201) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if status == 201:
            return httpx.Response(201, json={"data": {"jobId": 42}})
        return httpx.Response(status, json={"message": "모집 마감일이 지났습니다"})

    monkeypatch.setattr(spring, "transport", httpx.MockTransport(handle))
    return seen


def overrides(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row["field_name"]): str(row["value"])
        for row in conn.execute("SELECT field_name, value FROM job_field_overrides")
    }


def test_고치기_모드는_칸을_입력으로_바꾼다(client: TestClient) -> None:
    html = client.get("/ui/review/jobs/1/edit").text

    assert 'name="region"' in html
    assert '<select name="employment_type"' in html
    assert "저장하고 오공고로 보내기" in html
    # 모회사는 크롤러 설정에서 오는 값이라 여기서 고치지 않는다
    assert 'name="parent_company_name"' not in html


def test_고친_칸만_사람_보정으로_남고_다시_정규화된다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    html = client.post(
        "/ui/review/jobs/1/edit",
        data={
            "title": "백엔드 개발자",  # 그대로
            "region": "서울 강남구",
            "employment_type": "CONTRACT",
            "responsibilities": "결제 서버를 만든다",
        },
    ).text

    assert overrides(conn) == {
        "region": "서울 강남구",
        "employment_type": "CONTRACT",
        "responsibilities": "결제 서버를 만든다",
    }
    row = conn.execute(
        "SELECT region, employment_type FROM normalized_jobs WHERE id = 1"
    ).fetchone()
    assert (row["region"], row["employment_type"]) == ("서울 강남구", "CONTRACT")
    assert "3칸을 고쳤다" in html
    assert "직접 수정" in html


def test_목록_밖_값은_받지_않는다(client: TestClient, conn: sqlite3.Connection) -> None:
    html = client.post("/ui/review/jobs/1/edit", data={"employment_type": "WHATEVER"}).text

    assert "목록 밖 값" in html
    assert overrides(conn) == {}


def test_저장하고_보내기는_고친_뒤_오공고에_등록한다(
    client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = oggonggo(monkeypatch)

    html = client.post("/ui/review/jobs/1/edit", data={"region": "판교", "send": "1"}).text

    assert len(seen) == 1
    assert json.loads(seen[0].content)["region"] == "판교"
    assert "오공고로 보냈다" in html
    status = conn.execute("SELECT status FROM spring_deliveries WHERE source_url = 'https://x/1'")
    assert status.fetchone()["status"] == "sent"


def test_패널의_보내기는_꺼져_있어도_보내고_거절_사유를_적는다(
    client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    oggonggo(monkeypatch, status=400)

    html = client.post("/ui/review/jobs/1/send").text

    assert "오공고가 거절했다" in html
    assert "모집 마감일이 지났습니다" in html
    assert "오공고가 거절했어요" in html  # 패널 위 실패 상자


def test_이미_보낸_공고는_다시_보내지_않는다(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = oggonggo(monkeypatch)

    html = client.post("/ui/review/jobs/2/send").text

    assert seen == []
    assert "이미 보낸 공고" in html


def test_표에서_고른_공고를_한꺼번에_보낸다(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = oggonggo(monkeypatch)

    response = client.post("/ui/review/send", data={"raw_job_id": ["1", "2"]})

    assert len(seen) == 1
    assert "1건 보냄" in response.text
    assert "이미 보낸 1건은 건너뜀" in response.text
    assert response.headers["HX-Trigger"] == "jobs-deleted"


def test_주소가_없으면_보내지_않고_사유를_적는다(
    client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = oggonggo(monkeypatch)
    write_config(conn, DeliverConfig(url="", enabled=False))

    html = client.post("/ui/review/jobs/1/send").text

    assert seen == []
    assert "오공고 주소가 비어 있다" in html


def test_AI로_다시_채우지_못하면_사유를_적는다(client: TestClient) -> None:
    """테스트 환경에는 AI 키가 없다. 호출 전에 실패하고, 그 사유가 패널에 남는다."""
    html = client.post("/ui/review/jobs/1/reclassify").text

    assert "AI 로 다시 채우지 못했다" in html
