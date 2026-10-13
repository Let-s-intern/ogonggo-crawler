"""렛커 콘텐츠 화면 (2026-10-13, LC-3457).

오공고 서버를 부르지 않는다. `httpx.MockTransport` 로 바꿔 끼운다
(`app/api/ui_letscareer_contents.py`).

| 확인 | 깨지면 |
|---|---|
| 위 메뉴에 켜지고 태그가 한글로 나온다 | 어떤 자료에 어떤 태그가 붙었는지 볼 곳이 없다 |
| 종류·직군·태그 여부로 거른다 | 태그 없는 콘텐츠만 골라 볼 수 없다 |
| 오공고가 답하지 않으면 사유를 띄운다 | 빈 목록과 장애가 구분되지 않는다 |
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db
from app.api.settings import get_connection
from app.config import Settings, get_settings
from app.deliver import settings as deliver_store
from app.job_analysis import ogonggo as job_analysis_ogonggo
from app.main import app

CONTENTS: list[dict[str, Any]] = [
    {
        "contentId": 1,
        "kind": "MATERIAL",
        "title": "마케팅 취준 총정리 자료집",
        "path": "/library/57/x",
        "tagged": True,
        "jobFields": ["MARKETING_ADVERTISING"],
        "topics": ["CAREER_START"],
        "taggedAt": "2026-10-13T11:12:00",
    },
    {
        "contentId": 2,
        "kind": "BLOG",
        "title": "GSAT 인적성 준비",
        "path": "/blog/240",
        "tagged": True,
        "jobFields": [],
        "topics": ["WRITTEN_TEST", "LARGE_COMPANY"],
        "taggedAt": "2026-10-13T11:13:00",
    },
    {
        "contentId": 3,
        "kind": "CHALLENGE",
        "title": "이력서 1주 완성 챌린지",
        "path": "/program/challenge/412",
        "tagged": False,
        "jobFields": [],
        "topics": [],
        "taggedAt": None,
    },
]


@pytest.fixture
def client(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    path = tmp_path / "jobs.db"
    connection = db.connect(path)
    db.migrate_up(connection)
    deliver_store.write_config(connection, deliver_store.DeliverConfig(url="https://ogonggo.test"))
    connection.close()

    def request_connection() -> Iterator[sqlite3.Connection]:
        opened = db.connect(path)
        try:
            yield opened
        finally:
            opened.close()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/internal/lets-career-contents"
        assert request.headers["X-Internal-Api-Key"] == "key"
        return httpx.Response(200, json={"status": 200, "message": "ok", "data": CONTENTS})

    monkeypatch.setattr(job_analysis_ogonggo, "transport", httpx.MockTransport(handler))
    app.dependency_overrides[get_connection] = request_connection
    app.dependency_overrides[get_settings] = lambda: Settings(ogonggo_internal_api_key="key")
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_위_메뉴에_켜지고_콘텐츠마다_태그가_한글로_나온다(client: TestClient) -> None:
    page = client.get("/letscareer-contents").text
    panel = client.get("/ui/letscareer-contents").text

    assert 'aria-current="page"' in page and "렛커 콘텐츠" in page
    assert (
        "마케팅 취준 총정리 자료집" in panel
        and "마케팅·광고" in panel
        and "취업 준비 시작" in panel
    )
    assert "인적성·필기·코딩테스트" in panel and "공통" in panel
    assert "https://www.letscareer.co.kr/library/57/x" in panel


@pytest.mark.parametrize(
    ("query", "shown", "hidden"),
    [
        ("kind=BLOG", "GSAT 인적성 준비", "마케팅 취준 총정리 자료집"),
        ("field=MARKETING_ADVERTISING", "마케팅 취준 총정리 자료집", "GSAT 인적성 준비"),
        ("field=COMMON", "GSAT 인적성 준비", "이력서 1주 완성 챌린지"),
        ("status=untagged", "이력서 1주 완성 챌린지", "GSAT 인적성 준비"),
    ],
)
def test_종류_직군_태그_여부로_거른다(
    client: TestClient, query: str, shown: str, hidden: str
) -> None:
    panel = client.get(f"/ui/letscareer-contents?{query}").text

    assert shown in panel and hidden not in panel


def test_오공고가_답하지_않으면_사유를_띄운다(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        job_analysis_ogonggo,
        "transport",
        httpx.MockTransport(lambda request: httpx.Response(503)),
    )

    panel = client.get("/ui/letscareer-contents").text

    assert "오공고에서 받지 못했어요" in panel and "503" in panel
