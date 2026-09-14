"""대시보드 화면. 처음 들어오면 보이는 화면이다.

실사이트에 나가지 않는다. 저장된 행을 시각을 통제해 넣고 화면 경로로만 연다.

| 확인 | 깨지면 |
|---|---|
| 네비게이션 맨 앞에 대시보드가 있다 | 진입 화면을 찾을 방법이 없다 |
| 화면이 열리고 조각 둘(지표, 로그)을 부른다 | 지표·로그가 안 뜬다 |
| 오늘 추가·완성 건수가 실제 시각과 같다 | 지표가 거짓말이 된다 |
| 완성 건수가 완성 공고 화면과 같은 정의를 쓴다 | 화면마다 다른 완성 기준이 생긴다 |
| 최근 완성 공고가 최신순으로 몇 건만 나온다 | 대시보드가 무한 목록이 된다 |
| 자주 쓰는 화면 바로가기가 있다 | 화면 이동이 느려진다 |
| 로그 조각이 방금 남긴 로그를 보여준다 | 실시간 로그가 실은 안 도는 장식이 된다 |
"""

from __future__ import annotations

import logging
import pathlib
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api.settings import get_connection
from app.api.ui import NAV
from app.main import app
from app.normalize.rules import NORMALIZED_FIELDS

KST = ZoneInfo("Asia/Seoul")
LIST_URL = "https://example.test/jobs/"


def _to_db(moment_kst: datetime) -> str:
    """KST 시각을 DB 가 쓰는 UTC naive 문자열로. `datetime('now')` 와 같은 모양이다."""
    return moment_kst.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


# 정오를 기준으로 한다. 자정 근처가 아니라 테스트가 언제 돌아도 "오늘"/"어제" 경계가
# 안 흔들린다
_NOW_KST = datetime.now(KST).replace(hour=12, minute=0, second=0, microsecond=0)
TODAY = _to_db(_NOW_KST)
YESTERDAY = _to_db(_NOW_KST - timedelta(days=1))


def add_raw(conn: sqlite3.Connection, raw_job_id: int, crawled_at: str) -> None:
    conn.execute(
        """
        INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash, crawled_at)
        VALUES (?, 1, ?, '{}', ?, ?)
        """,
        (raw_job_id, f"{LIST_URL}{raw_job_id}/", f"hash-{raw_job_id}", crawled_at),
    )


def add_complete(
    conn: sqlite3.Connection,
    raw_job_id: int,
    *,
    normalized_at: str,
    classified_at: str | None = None,
) -> None:
    """열여섯 칸을 전부 채운 완성 행. `classified_at` 을 주면 그 시각으로 분류 행도 만든다."""
    values = {name: f"값-{name}" for name in NORMALIZED_FIELDS}
    values["title"] = f"공고 {raw_job_id}"
    values["company_name"] = "엘지전자"
    columns = list(NORMALIZED_FIELDS)
    conn.execute(
        f"""
        INSERT INTO normalized_jobs
               (raw_job_id, source_url, parent_company_name, normalized_at, {", ".join(columns)})
        VALUES (?, ?, 'LG', ?, {", ".join("?" for _ in columns)})
        """,
        (
            raw_job_id,
            f"{LIST_URL}{raw_job_id}/",
            normalized_at,
            *(values[name] for name in columns),
        ),
    )
    if classified_at is not None:
        conn.execute(
            "INSERT INTO job_classifications (raw_job_id, model, classified_at)"
            " VALUES (?, 'test', ?)",
            (raw_job_id, classified_at),
        )


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute(
        "INSERT INTO crawlers (name, list_url, status, default_company)"
        " VALUES ('lg', ?, 'promoted', 'LG')",
        (LIST_URL,),
    )
    connection.execute("INSERT INTO workflows (crawler_id, name) VALUES (1, 'lg')")
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

    app.dependency_overrides[get_connection] = request_connection
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_네비게이션_맨_앞이_대시보드다() -> None:
    assert NAV[0] == ("/", "대시보드")


def test_화면이_열리고_조각_둘을_부른다(client: TestClient) -> None:
    body = client.get("/").text

    assert 'hx-get="/ui/dashboard"' in body
    assert 'hx-get="/ui/dashboard/logs"' in body


def test_오늘_추가_건수가_실제와_같다(client: TestClient, conn: sqlite3.Connection) -> None:
    add_raw(conn, 1, TODAY)
    add_raw(conn, 2, TODAY)
    add_raw(conn, 3, YESTERDAY)
    conn.commit()

    body = client.get("/ui/dashboard").text

    assert "오늘 추가" in body
    idx = body.index("오늘 추가")
    assert ">2<" in body[idx : idx + 200]


def test_오늘_완성_건수는_분류_시각_기준_근사다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """`classified_at` 이 오늘이면 `normalized_at` 이 어제라도 오늘로 센다."""
    add_raw(conn, 1, YESTERDAY)
    add_complete(conn, 1, normalized_at=YESTERDAY, classified_at=TODAY)
    conn.commit()

    body = client.get("/ui/dashboard").text

    idx = body.index("오늘 완성")
    assert ">1<" in body[idx : idx + 200]


def test_분류_시각이_없으면_정규화_시각으로_근사한다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    add_raw(conn, 1, TODAY)
    add_complete(conn, 1, normalized_at=TODAY, classified_at=None)
    conn.commit()

    body = client.get("/ui/dashboard").text

    idx = body.index("오늘 완성")
    assert ">1<" in body[idx : idx + 200]


def test_전체_완성_건수가_완성_공고_화면과_같은_정의를_쓴다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    from app.api.ui_complete import completed_count

    add_raw(conn, 1, TODAY)
    add_complete(conn, 1, normalized_at=TODAY)
    add_raw(conn, 2, TODAY)  # 정규화되지 않아 완성이 아니다
    conn.commit()

    assert completed_count(conn) == 1
    body = client.get("/ui/dashboard").text
    idx = body.index("전체 완성")
    assert ">1<" in body[idx : idx + 200]


def test_최근_완성_공고가_최신순으로_한도만큼만_나온다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    from app.api.ui_dashboard import RECENT_LIMIT

    for i in range(1, RECENT_LIMIT + 3):
        add_raw(conn, i, TODAY)
        add_complete(conn, i, normalized_at=TODAY)
    conn.commit()

    body = client.get("/ui/dashboard").text

    shown = [f"공고 {i}" in body for i in range(1, RECENT_LIMIT + 3)]
    assert sum(shown) == RECENT_LIMIT
    # 가장 최근(가장 큰 raw_job_id)이 보여야 한다
    assert f"공고 {RECENT_LIMIT + 2}" in body
    assert "공고 1" not in body


def test_최근_완성_공고_카드가_미리보기를_연다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    add_raw(conn, 1, TODAY)
    add_complete(conn, 1, normalized_at=TODAY)
    conn.commit()

    body = client.get("/ui/dashboard").text

    assert "data-modal-open" in body
    assert 'hx-get="/ui/complete/1/preview"' in body


def test_완성_공고가_없으면_안내를_적는다(client: TestClient) -> None:
    body = client.get("/ui/dashboard").text

    assert "아직 완성된 공고가 없다" in body


def test_더보기가_완성_공고_화면으로_간다(client: TestClient) -> None:
    body = client.get("/ui/dashboard").text

    assert 'href="/complete">더보기</a>' in body


def test_자주_쓰는_화면_바로가기가_있다(client: TestClient) -> None:
    body = client.get("/ui/dashboard").text

    for path, label, _ in [
        ("/complete", "완성 공고", ""),
        ("/review", "데이터 확인", ""),
        ("/taxonomy", "직무 분류", ""),
        ("/companies", "회사 로고", ""),
    ]:
        assert f'href="{path}"' in body
        assert label in body


def test_로그가_없으면_안내를_적는다(client: TestClient) -> None:
    from app.log_ring import handler

    handler.clear()

    assert "아직 남은 로그가 없다" in client.get("/ui/dashboard/logs").text


def test_로그_조각이_방금_남긴_로그를_보여준다(client: TestClient) -> None:
    probe = logging.getLogger("app.test_dashboard_probe")
    probe.info("대시보드 로그 확인용 문구 12345")

    body = client.get("/ui/dashboard/logs").text

    assert "대시보드 로그 확인용 문구 12345" in body
    assert "app.test_dashboard_probe" in body
    assert "INFO" in body


def test_방금_남긴_로그가_맨_위에_온다(client: TestClient) -> None:
    """계속 스크롤을 내려야 새 줄이 보이면 안 된다 — 최신이 늘 같은 자리(맨 위)여야 한다."""
    from app.log_ring import handler

    handler.clear()
    probe = logging.getLogger("app.test_dashboard_probe")
    probe.info("먼저 남긴 줄")
    probe.info("나중에 남긴 줄")

    body = client.get("/ui/dashboard/logs").text

    assert body.index("나중에 남긴 줄") < body.index("먼저 남긴 줄")


def test_토큰_사용량이_없으면_안내를_적는다(client: TestClient) -> None:
    body = client.get("/ui/dashboard").text

    assert "AI 토큰 사용량" in body
    assert "아직 쌓인 호출이 없다" in body


def test_토큰_사용량이_기능별로_나온다(client: TestClient, conn: sqlite3.Connection) -> None:
    from app.llm.base import Usage
    from app.llm.log import CLASSIFY, SELECTOR_GENERATE, record_call

    record_call(
        conn,
        feature=CLASSIFY,
        usage=Usage(
            provider="gemini",
            model="gemini-3.5-flash",
            input_tokens=1000,
            output_tokens=200,
            total_tokens=1200,
            latency_ms=500,
        ),
    )
    record_call(
        conn,
        feature=SELECTOR_GENERATE,
        usage=Usage(
            provider="gemini",
            model="gemini-3.5-flash",
            input_tokens=300,
            output_tokens=100,
            total_tokens=400,
            latency_ms=500,
        ),
    )
    conn.commit()

    body = client.get("/ui/dashboard").text

    assert "본문 분류" in body
    assert "1,200 토큰 · 호출 1회" in body
    assert "셀렉터 생성" in body
    assert "400 토큰 · 호출 1회" in body
    assert "AI 수정" in body  # 호출이 없어도 그래프에서 빠지지 않는다


def test_일별_추이에_토큰_사용량도_들어간다(client: TestClient, conn: sqlite3.Connection) -> None:
    from app.llm.base import Usage
    from app.llm.log import CLASSIFY, record_call

    record_call(
        conn,
        feature=CLASSIFY,
        usage=Usage(
            provider="gemini",
            model="gemini-3.5-flash",
            input_tokens=500,
            output_tokens=500,
            total_tokens=1000,
            latency_ms=100,
        ),
    )
    conn.commit()

    body = client.get("/ui/dashboard").text

    assert "AI 토큰(이 기간 중 최댓값 기준, 추가·완성과 축이 다르다)" in body
    assert "토큰 1,000" in body  # 오늘 칸의 title 속성에 오늘 쓴 토큰 수가 그대로 적힌다


def test_오늘_AI_토큰_지표가_있다(client: TestClient, conn: sqlite3.Connection) -> None:
    from app.llm.base import Usage
    from app.llm.log import CLASSIFY, record_call

    record_call(
        conn,
        feature=CLASSIFY,
        usage=Usage(
            provider="gemini",
            model="gemini-3.5-flash",
            input_tokens=800,
            output_tokens=200,
            total_tokens=1000,
            latency_ms=100,
        ),
    )
    conn.commit()

    body = client.get("/ui/dashboard").text

    assert "오늘 AI 토큰" in body
    idx = body.index("오늘 AI 토큰")
    assert "1,000" in body[idx : idx + 200]


def test_예상_비용이_참고_모델_단가로_계산된다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    """`app.api.ui_dashboard.COST_REFERENCE_MODEL` 의 입력·출력 단가로 어림잡는다."""
    from app.api.ui_dashboard import COST_REFERENCE_MODEL
    from app.llm.base import Usage
    from app.llm.log import CLASSIFY, record_call

    record_call(
        conn,
        feature=CLASSIFY,
        usage=Usage(
            provider="gemini",
            model="gemini-3.5-flash",
            # 입력 100만 토큰 * $0.25 + 출력 100만 토큰 * $1.50 = $1.75
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            total_tokens=2_000_000,
            latency_ms=100,
        ),
    )
    conn.commit()

    body = client.get("/ui/dashboard").text

    assert COST_REFERENCE_MODEL in body
    assert "$1.7500" in body
    assert "2,415원" in body  # $1.75 * 1,380원/달러(app.api.ui_dashboard._KRW_PER_USD)


def test_호출이_없으면_예상_비용은_0이다(client: TestClient) -> None:
    body = client.get("/ui/dashboard").text

    assert "$0.0000" in body
    assert "0원" in body
