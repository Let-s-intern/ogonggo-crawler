"""API 설정 수동 보정 (2.3).

지금까지 `crawlers.api_config_json` 을 쓰는 경로는 등록 한 곳뿐이었다. 매핑을 고치려면 다시
등록하거나 DB 를 직접 고치는 수밖에 없었고, 둘 다 나쁘다 — 다시 등록하면 경로 판정이 다시
돌아 브라우저와 모델을 쓰고, 직접 고치면 스키마 검증을 지나지 않는다.

`PUT /api/crawlers/{id}/selectors` 의 API 판이다. 보는 것은 셋 — 저장되는가, 검증을
지나는가, 깨진 설정을 사유와 함께 거절하는가.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import crawlers as crawlers_api
from app.main import app

LIST_URL = "https://careers.lg.com/apply"

CONFIG: dict[str, Any] = {
    "list": {
        "url": "https://api.careers.lg.com/rmk/job/retrieveJobNoticesList",
        "method": "POST",
        "body": {},
        "items_path": "data.jobNoticeList",
        "fields": {"title": "jobNoticeName", "date": "recEndDateTime"},
        "id_field": "jobNoticeId",
        "link_template": "https://careers.lg.com/apply/detail?id={id}",
    },
    "detail": {
        "url": "https://api.careers.lg.com/rmk/job/retrieveJobNoticesDetail",
        "method": "POST",
        "body": {"jobNoticeId": "{id}"},
        "fields": {
            "title": "data.jobNoticesDetail.jobNoticesDetail.jobNoticeName",
            "body": "data.jobNoticesDetail.recList.*.detailContext",
            "region": "data.jobNoticesDetail.recList.*.locationName",
        },
    },
}


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
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


def make_crawler(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        "INSERT INTO crawlers (name, list_url, status, list_mode, detail_mode) "
        "VALUES ('LG', ?, 'promoted', 'api', 'api')",
        (LIST_URL,),
    )
    return int(cursor.lastrowid or 0)


def stored(conn: sqlite3.Connection, crawler_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT api_config_json FROM crawlers WHERE id = ?", (crawler_id,)
    ).fetchone()
    return dict(json.loads(str(row["api_config_json"])))


def test_a_new_mapping_is_stored(client: TestClient, conn: sqlite3.Connection) -> None:
    crawler_id = make_crawler(conn)

    response = client.put(f"/api/crawlers/{crawler_id}/api-config", json=CONFIG)

    assert response.status_code == 200
    saved = stored(conn, crawler_id)
    assert saved["detail"]["fields"]["region"] == ("data.jobNoticesDetail.recList.*.locationName")


def test_the_status_is_not_touched_by_a_mapping_fix(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """보정은 보정이다. 승격된 크롤러가 보정 한 번으로 초안이 되지 않는다."""
    crawler_id = make_crawler(conn)

    body = client.put(f"/api/crawlers/{crawler_id}/api-config", json=CONFIG).json()

    assert body["status"] == "promoted"
    row = conn.execute("SELECT status FROM crawlers WHERE id = ?", (crawler_id,)).fetchone()
    assert str(row["status"]) == "promoted"


def test_a_field_the_schema_does_not_have_is_refused(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """읽는 쪽은 설정이 이미 검증됐다고 믿는다. 여기서 막지 않으면 실행 중에야 깨진다."""
    crawler_id = make_crawler(conn)
    broken = json.loads(json.dumps(CONFIG))
    broken["detail"]["fields"]["salary"] = "data.item.pay"

    response = client.put(f"/api/crawlers/{crawler_id}/api-config", json=broken)

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "unknown_field"
    assert (
        conn.execute("SELECT api_config_json FROM crawlers WHERE id = ?", (crawler_id,)).fetchone()[
            "api_config_json"
        ]
        is None
    )


def test_a_link_template_without_an_id_is_refused(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    crawler_id = make_crawler(conn)
    broken = json.loads(json.dumps(CONFIG))
    broken["list"]["link_template"] = "https://careers.lg.com/apply/detail"

    response = client.put(f"/api/crawlers/{crawler_id}/api-config", json=broken)

    assert response.status_code == 422
    assert response.json()["detail"]["reason"] == "missing_field"


def test_updating_a_missing_crawler_is_404(client: TestClient) -> None:
    response = client.put("/api/crawlers/999/api-config", json=CONFIG)

    assert response.status_code == 404
