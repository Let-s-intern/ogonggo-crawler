"""공고 목록과 오른쪽 상세 패널 (2026-09-15).

크롤러에서는 사람이 값을 고치지 않는다. 목록은 읽기 전용이고, 제목을 누르면 오른쪽 패널이
오공고로 보내는 칸을 빈 칸까지 모두 보인다. 오공고 쪽은 보냈는지만 적는다.

| 확인 | 깨지면 |
|---|---|
| 공고 목록 화면이 조건·표 조각을 부른다 | 화면이 빈다 |
| 옛 완성 공고 주소가 공고 목록으로 간다 | 북마크가 죽는다 |
| 표에 오공고 전송 여부가 칩으로 나온다 | 보냈는지 목록에서 모른다 |
| 패널이 보내는 칸을 모두 보이고 빈 칸은 `비어 있음` 이다 | 무엇이 빈 채로 가는지 모른다 |
| 나눈 공고는 번호마다 따로 연다 | 형제 공고의 값이 섞인다 |
| AI 채움률이 칸마다 계산된다 | AI 가 못 채우는 칸을 모른다 |
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
from app.classify.schema import VALUE_LABELS
from app.main import app

LIST_URL = "https://www.skcareers.com/Recruit/Detail/"


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES ('sk', ?, 'promoted')", (LIST_URL,)
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, 'SK')")
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, '나눈 공고')")
    for raw_job_id, workflow_id in ((1, 1), (2, 1), (3, 2)):
        connection.execute(
            """
            INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)
            VALUES (?, ?, ?, '{}', ?)
            """,
            (raw_job_id, workflow_id, f"{LIST_URL}{raw_job_id}", f"hash-{raw_job_id}"),
        )
    # 1: 칸이 거의 다 찬 공고이고 오공고로 보냈다
    connection.execute(
        """
        INSERT INTO normalized_jobs (
            id, raw_job_id, source_url, company_name, parent_company_name, title, job_field,
            job_role, industry, employment_type, experience_type, experience_min_years,
            education_level, recruitment_type, recruitment_start_at, recruitment_end_at,
            closes_when_filled, auto_close_enabled, application_method, responsibilities,
            qualifications, hiring_process)
        VALUES (1, 1, ?, 'SK intellix', 'SK', 'AI Engineer 경력 채용', '개발', 'AI 엔지니어',
                'IT·정보통신업', 'FULL_TIME', 'EXPERIENCED', '5', 'BACHELOR', 'PERIOD',
                '2026-08-12 00:00:00', '2099-08-26 23:59:59', 'false', 'true', 'EXTERNAL_PAGE',
                'LLM 기반 AI 서비스 개발', '실무 경력 5년 이상', '서류 → 면접')
        """,
        (f"{LIST_URL}1",),
    )
    connection.execute(
        "INSERT INTO spring_deliveries (source_url, spring_job_id, status, sent_at)"
        " VALUES (?, 2, 'sent', '2026-09-15 02:17:09')",
        (f"{LIST_URL}1",),
    )
    # 2: 제목만 있는 공고. 아직 보내지 않았다
    connection.execute(
        "INSERT INTO normalized_jobs (id, raw_job_id, source_url, title)"
        " VALUES (2, 2, ?, '분류 전 공고')",
        (f"{LIST_URL}2",),
    )
    # 3: 한 원문을 둘로 나눈 공고
    for normalized_id, part, title in ((3, 1, "나눈 공고 첫째"), (4, 2, "나눈 공고 둘째")):
        connection.execute(
            "INSERT INTO normalized_jobs (id, raw_job_id, part, source_url, title)"
            " VALUES (?, 3, ?, ?, ?)",
            (normalized_id, part, f"{LIST_URL}3#{part}", title),
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
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides.clear()


def row_of(html: str, title: str) -> str:
    """표에서 그 제목이 있는 행 하나."""
    start = html.rindex("<tr>", 0, html.index(title))
    return html[start : html.index("</tr>", start)]


def test_공고_목록_화면이_조건과_표_조각을_부른다(client: TestClient) -> None:
    body = client.get("/review").text

    assert "<title>공고 목록" in body
    assert 'hx-get="/ui/review/filters"' in body
    assert 'hx-get="/ui/review"' in body
    assert '<dialog id="app-panel"' in body


@pytest.mark.parametrize("old", ["/complete", "/jobs"])
def test_옛_주소는_공고_목록으로_간다(client: TestClient, old: str) -> None:
    response = client.get(old)

    assert response.status_code == 307
    assert response.headers["location"] == "/review"


def test_표에_오공고_전송_여부가_칩으로_나온다(client: TestClient) -> None:
    html = client.get("/ui/review").text

    assert "전송함" in row_of(html, "AI Engineer 경력 채용")
    assert "전송 안 함" in row_of(html, "분류 전 공고")


def test_제목을_누르면_오른쪽_패널이_그_공고를_연다(client: TestClient) -> None:
    html = client.get("/ui/review").text

    row = row_of(html, "AI Engineer 경력 채용")
    assert "data-panel-open" in row
    assert 'hx-get="/ui/review/jobs/1/panel"' in row
    assert 'hx-target="#app-panel-body"' in row
    # 목록에서 값을 고치는 입구는 없다
    assert "수정" not in html


def test_패널이_보내는_칸을_한글_이름으로_모두_보인다(client: TestClient) -> None:
    html = client.get("/ui/review/jobs/1/panel").text

    for label in (
        "직군",
        "직무",
        "산업",
        "고용 형태",
        "경력",
        "학력",
        "근무 지역",
        "모집 인원",
        "모집 유형",
        "모집 시작",
        "모집 마감",
        "지원 방법",
        "채용 시 마감",
        "자동 종료",
        "대표 이미지",
    ):
        assert f">{label}</dt>" in html
    assert VALUE_LABELS["employment_type"]["FULL_TIME"] in html
    assert f"{VALUE_LABELS['experience_type']['EXPERIENCED']} · 5년 이상" in html
    assert VALUE_LABELS["education_level"]["BACHELOR"] in html
    assert VALUE_LABELS["application_method"]["EXTERNAL_PAGE"] in html
    assert "2026-08-12 00:00" in html
    assert "LLM 기반 AI 서비스 개발" in html


def test_빈_칸은_비어_있음으로_보이고_빈_본문_칸을_적는다(client: TestClient) -> None:
    full = client.get("/ui/review/jobs/1/panel").text
    blank = client.get("/ui/review/jobs/2/panel").text

    assert "비어 있음" in full  # 근무 지역·모집 인원·대표 이미지
    assert blank.count("비어 있음") == 15
    assert "비어 있는 본문 칸:" in full
    assert "급여·처우" in full[full.index("비어 있는 본문 칸:") :]


def test_패널은_오공고로_보냈는지만_적는다(client: TestClient) -> None:
    sent = client.get("/ui/review/jobs/1/panel").text
    unsent = client.get("/ui/review/jobs/2/panel").text

    assert "오공고 전송함" in sent
    assert "2026-09-15 11:17:09" in sent  # UTC 02:17 을 표시 시간대로 그린다
    assert "오공고 전송 안 함" in unsent
    assert "검수" not in sent


def test_나눈_공고는_번호마다_따로_연다(client: TestClient) -> None:
    html = client.get("/ui/review/jobs/4/panel").text

    assert "나눈 공고 둘째" in html
    assert "나눈 공고 첫째" not in html


def test_없는_공고는_사유를_적는다(client: TestClient) -> None:
    assert "공고를 찾지 못했다" in client.get("/ui/review/jobs/999/panel").text


def test_패널의_삭제가_그_수집_건으로_확인_창을_연다(client: TestClient) -> None:
    html = client.get("/ui/review/jobs/1/panel").text

    assert 'hx-post="/ui/review/delete/confirm"' in html
    assert '"raw_job_id": "1"' in html
    assert "원문 열기" in html


def test_AI_채움률이_지금_조건의_공고로_계산된다(client: TestClient) -> None:
    """워크플로우 1 의 두 건 중 주요 업무는 한 건만 찼다."""
    html = client.get("/ui/review", params={"workflow_id": "1"}).text

    found = re.search(r"주요 업무</span>.*?(\d+)%</span>", html, re.DOTALL)
    assert found is not None
    assert found.group(1) == "50"
    assert "지금 조건의 2건 기준" in html
