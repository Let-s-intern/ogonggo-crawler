"""AI 공고 분석 화면 (2026-10-07, LC-3446).

모델을 부르지 않는다. 시험 분석은 가짜 제공자가 답한다 (`app/api/ui_job_analysis.py`).

| 확인 | 깨지면 |
|---|---|
| 설정 묶음에서 켜지고 기본 방법이 채워진다 | 고칠 자리를 못 찾거나 빈 폼에서 시작한다 |
| 저장하면 새 판이 생기고 올린 파일이 판에 들어간다 | 저장이나 파일이 조용히 사라진다 |
| 빼기를 고른 파일은 다음 판에 없다 | 지운 파일이 프롬프트에 계속 실린다 |
| 받을 수 없는 파일은 친 내용과 함께 사유를 댄다 | 고친 지침이 사유와 함께 날아간다 |
| 시험은 저장 안 한 지침으로 돌고 오공고 화면 짜임으로 그린다 | 저장해야만 결과를 볼 수 있다 |
| 비교하면 지금 판으로도 한 번 더 돈다 | 무엇이 달라졌는지 볼 수 없다 |
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api.settings import get_connection
from app.job_analysis import analyzer, guide
from app.main import app
from tests.test_job_analysis import FakeProvider, answer


@pytest.fixture
def db_path(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "jobs.db"
    connection = db.connect(path)
    db.migrate_up(connection)
    connection.execute("INSERT INTO crawlers (id, name, list_url) VALUES (1, 'x', 'https://x')")
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, 'x')")
    connection.execute(
        "INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)"
        " VALUES (1, 1, 'https://x/1', '{}', 'h1')"
    )
    connection.execute(
        "INSERT INTO normalized_jobs (id, raw_job_id, source_url, company_name, title,"
        " qualifications) VALUES (7, 1, 'https://x/1', 'PFCT', 'PO 인턴',"
        " '모호한 요구 사항 속에서 핵심 문제를 정의하고 해결책을 도출하는 분')"
    )
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


def form_of(value: guide.Guide, **extra: Any) -> dict[str, Any]:
    """화면 폼이 보내는 모양. 남긴 파일은 이름과 내용이 같은 순서로 되풀이된다."""
    data: dict[str, Any] = {"system": value.system, **extra}
    for key, _, _ in guide.SECTIONS:
        data[f"section__{key}"] = value.sections[key]
    data["file_name"] = [item.name for item in value.files]
    data["file_content"] = [item.content for item in value.files]
    return data


def use(monkeypatch: pytest.MonkeyPatch, provider: FakeProvider) -> None:
    monkeypatch.setattr(analyzer, "for_feature", lambda feature, settings: (provider, "fake-model"))


def test_설정_묶음에서_켜지고_기본_방법이_채워진다(client: TestClient) -> None:
    page = client.get("/job-analysis").text
    fragment = client.get("/ui/job-analysis").text

    assert 'href="/job-analysis"' in page and "AI 공고 분석" in page
    assert "판 0" in fragment
    assert "입사하면 실제로 하는 일을 3개까지 고른다" in fragment
    assert "PFCT · PO 인턴" in fragment


def test_저장하면_올린_파일이_새_판에_들어간다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    response = client.put(
        "/ui/job-analysis",
        data=form_of(guide.DEFAULT_GUIDE, note="역량 가이드 추가"),
        files=[("upload", ("역량.md", "# 기획 역량\n문제 정의".encode(), "text/markdown"))],
    )

    assert "판 1 로 저장했어요" in response.text
    saved = guide.current(conn)
    assert saved.note == "역량 가이드 추가"
    assert saved.guide.files == (guide.GuideFile("역량.md", "# 기획 역량\n문제 정의"),)
    assert "역량.md" in response.text


def test_빼기를_고른_파일은_다음_판에_없다(client: TestClient, conn: sqlite3.Connection) -> None:
    first = guide.save(
        conn,
        guide.with_files(guide.DEFAULT_GUIDE, (guide.GuideFile("a.md", "가"),)),
    )

    client.put("/ui/job-analysis", data=form_of(first.guide, remove_file="a.md"))

    assert guide.current(conn).number == 2
    assert guide.current(conn).guide.files == ()


def test_받을_수_없는_파일은_친_내용과_함께_사유를_댄다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    edited = guide.Guide("고친 지시", dict(guide.DEFAULT_GUIDE.sections))

    response = client.put(
        "/ui/job-analysis",
        data=form_of(edited),
        files=[("upload", ("guide.pdf", b"%PDF", "application/pdf"))],
    )

    assert "file_type" in response.text
    assert "고친 지시" in response.text
    assert "아직 저장하지 않은 내용이 있어요" in response.text
    assert guide.current(conn).number == 0


def test_시험은_저장_안_한_지침으로_돌고_오공고_짜임으로_그린다(
    client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider(answer())
    use(monkeypatch, provider)
    edited = guide.Guide("저장 안 한 지시", dict(guide.DEFAULT_GUIDE.sections))

    response = client.post(
        "/ui/job-analysis/try",
        data=form_of(edited, source="job", job_id="7"),
        files=[("upload", ("메모.txt", "새 파일 내용".encode(), "text/plain"))],
    )

    html = response.text
    assert provider.systems == ["저장 안 한 지시"]
    assert "새 파일 내용" in provider.prompts[0]
    assert "오공고 체크포인트" in html and "공고 속 문구" in html
    assert "공고에 명시 없음" in html
    assert guide.current(conn).number == 0


def test_원문을_붙여_넣어_시험한다(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FakeProvider(answer())
    use(monkeypatch, provider)

    client.post(
        "/ui/job-analysis/try",
        data=form_of(
            guide.DEFAULT_GUIDE,
            source="paste",
            paste_title="고용24 공고",
            paste_body="모호한 요구 사항 속에서 핵심 문제를 정의하고",
        ),
    )

    assert "## 본문\n모호한 요구 사항" in provider.prompts[0]


def test_비교하면_지금_판으로도_돈다(
    client: TestClient, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    guide.save(conn, guide.Guide("판 1 지시", dict(guide.DEFAULT_GUIDE.sections)))
    provider = FakeProvider(answer(), answer())
    use(monkeypatch, provider)

    response = client.post(
        "/ui/job-analysis/try",
        data=form_of(guide.DEFAULT_GUIDE, source="job", job_id="7", compare="1"),
    )

    assert provider.systems == [guide.DEFAULT_GUIDE.system, "판 1 지시"]
    assert "고친 방법" in response.text and "지금 판 1" in response.text
    versions = [row[0] for row in conn.execute("SELECT rules_version FROM llm_calls ORDER BY id")]
    assert versions == [None, 1]


def test_공고를_고르지_않은_시험은_사유를_댄다(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = FakeProvider()
    use(monkeypatch, provider)

    response = client.post("/ui/job-analysis/try", data=form_of(guide.DEFAULT_GUIDE, source="job"))

    assert "no_posting" in response.text
    assert provider.prompts == []
