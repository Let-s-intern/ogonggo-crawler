"""제공 API 의 시각은 UTC 그대로다 (21.3).

화면은 운영자가 사는 시간대로 그린다. 제공 API 는 그렇게 하지 않는다. `normalized_at` 은 소비
측의 폴링 커서라, 값이 9시간 밀리면 이미 받은 것을 다시 받거나 못 받은 구간이 생긴다
(`.claude/docs/api-contract.md`).

같은 행을 두 곳에서 꺼내 비교한다. 한쪽만 보면 "어느 쪽이 맞는가" 를 판단할 수 없고, 나중에
화면 값을 보고 API 도 그럴 것이라 짐작하는 일이 생긴다.

| 확인 | 깨지면 |
|---|---|
| 응답의 `normalized_at` 이 UTC ISO 형식이다 | 소비 측 커서가 시차만큼 어긋난다 |
| 같은 행의 화면 값이 9시간 뒤다 | 화면이 UTC 로 돌아간 것이다 |
| 두 값이 같은 순간을 가리킨다 | 한쪽이 저장된 값을 바꿔 쓰고 있다 |
"""

from __future__ import annotations

import pathlib
import re
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import crawlers as crawlers_api
from app.api import jobs as jobs_api
from app.api import ui
from app.config import get_settings
from app.main import app

# 저장 형식. SQLite `datetime('now')` 가 만드는 UTC 초 단위 문자열이다
STORED = "2026-08-21 10:00:00"
SEOUL = ZoneInfo("Asia/Seoul")
SHOWN = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [A-Z]+")


@pytest.fixture(autouse=True)
def seoul(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DISPLAY_TIMEZONE", "Asia/Seoul")
    get_settings.cache_clear()
    ui._zone.cache_clear()
    yield
    get_settings.cache_clear()
    ui._zone.cache_clear()


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status)"
        " VALUES ('시드', 'https://example.test/', 'promoted')"
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '시드')")
    connection.execute(
        """
        INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash, crawled_at)
        VALUES (1, 'https://example.test/jobs/1', '{}', 'hash-1', ?)
        """,
        (STORED,),
    )
    connection.execute(
        """
        INSERT INTO normalized_jobs
               (raw_job_id, company_name, title, recruitment_end_at, body, qualifications,
                source_url, normalized_at)
        VALUES (1, '회사', '공고', '2026-09-30', '본문', '자격요건',
                'https://example.test/jobs/1', ?)
        """,
        (STORED,),
    )
    connection.commit()
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

    # 제공 API 와 화면이 같은 DB 를 본다. 두 경로가 같은 행을 꺼내야 비교가 성립한다
    app.dependency_overrides[jobs_api.get_connection] = request_connection
    app.dependency_overrides[crawlers_api.get_connection] = request_connection
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_제공_API_의_normalized_at_은_UTC_다(client: TestClient) -> None:
    """계약이 요구하는 모양이다. 표시 시간대 설정이 이 값을 건드리지 않는다."""
    payload = client.get("/api/jobs").json()

    assert [item["normalized_at"] for item in payload["items"]] == ["2026-08-21T10:00:00Z"]


def test_같은_행이_화면에서는_9시간_뒤로_보인다(client: TestClient) -> None:
    """화면 값과 API 값이 다른 것은 정상이다. 다른 이유가 문서에 적혀 있어야 한다."""
    api_value = client.get("/api/jobs").json()["items"][0]["normalized_at"]
    html = client.get("/ui/review/jobs/1/panel").text

    found = SHOWN.search(html)
    assert found is not None, "공고 상세 패널에 수집 시각이 없다"
    screen_value = found.group()
    assert screen_value == "2026-08-21 19:00:00 KST"

    api_moment = datetime.fromisoformat(api_value.replace("Z", "+00:00"))
    screen_moment = datetime.strptime(screen_value[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=SEOUL)
    # 화면은 9시간 앞선 벽시계를 그리지만, 가리키는 순간은 같은 순간이다
    assert screen_moment == api_moment
    assert screen_moment.replace(tzinfo=None) - api_moment.replace(tzinfo=None) == timedelta(
        hours=9
    )


def test_표시_시간대를_바꿔도_API_는_그대로다(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """소비 측 커서가 운영 화면 설정에 흔들리지 않는다."""
    monkeypatch.setenv("DISPLAY_TIMEZONE", "America/New_York")
    get_settings.cache_clear()
    ui._zone.cache_clear()

    payload = client.get("/api/jobs").json()

    assert payload["items"][0]["normalized_at"] == "2026-08-21T10:00:00Z"
