"""대시보드 화면. 처음 들어오면 보이는 화면이다.

실사이트에 나가지 않는다. 저장된 행을 시각을 통제해 넣고 화면 경로로만 연다.

| 확인 | 깨지면 |
|---|---|
| 네비게이션 맨 앞에 대시보드가 있다 | 진입 화면을 찾을 방법이 없다 |
| 화면이 열리고 조각 둘(요약, 로그)을 부른다 | 요약·로그가 안 뜬다 |
| 오늘 새 공고·오공고 전송 건수가 저장된 시각과 같다 | 지표가 거짓말이 된다 |
| 처리 대기가 분류 대기와 전송 대기를 따로 센다 | 어디서 막혔는지 모른다 |
| 비용이 모델별 단가로 계산되고, 단가 없는 호출은 따로 적힌다 | 비용이 한 모델 가격으로 틀린다 |
| 확인이 필요한 것에 연속 실패·전송 실패·필수 칸 빈 공고가 뜬다 | 문제가 대시보드에서 안 보인다 |
| 최근 전송한 공고와 워크플로우 상태가 나온다 | 방금 무엇이 갔는지, 수집이 도는지 모른다 |
| 로그 조각이 방금 남긴 로그를 보여준다 | 실시간 로그가 실은 안 도는 장식이 된다 |
"""

from __future__ import annotations

import json
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
from app.llm import pricing
from app.llm.base import Usage
from app.llm.log import CLASSIFY, record_call
from app.main import app

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


def add_raw(conn: sqlite3.Connection, raw_job_id: int, crawled_at: str, body: str = "") -> None:
    conn.execute(
        """
        INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash, crawled_at)
        VALUES (?, 1, ?, ?, ?, ?)
        """,
        (
            raw_job_id,
            f"{LIST_URL}{raw_job_id}/",
            json.dumps({"body": body}, ensure_ascii=False),
            f"hash-{raw_job_id}",
            crawled_at,
        ),
    )


def add_job(
    conn: sqlite3.Connection,
    raw_job_id: int,
    *,
    education_level: str | None = "ANY",
    classified: bool = True,
) -> None:
    """정규화한 공고 한 건. 기본은 오공고 필수 칸이 다 찬 공고다."""
    conn.execute(
        """
        INSERT INTO normalized_jobs (raw_job_id, source_url, company_name, title, employment_type,
                                     experience_type, education_level, recruitment_type)
        VALUES (?, ?, '예시', ?, 'FULL_TIME', 'EXPERIENCED', ?, 'ALWAYS_OPEN')
        """,
        (raw_job_id, f"{LIST_URL}{raw_job_id}/", f"공고 {raw_job_id}", education_level),
    )
    if classified:
        conn.execute(
            "INSERT INTO job_classifications (raw_job_id, model, classified_at)"
            " VALUES (?, 'test', ?)",
            (raw_job_id, TODAY),
        )


def add_delivery(
    conn: sqlite3.Connection, raw_job_id: int, *, status: str, at: str, error: str = ""
) -> None:
    conn.execute(
        """
        INSERT INTO spring_deliveries
               (source_url, status, attempts, last_error, sent_at, updated_at)
        VALUES (?, ?, 1, ?, CASE WHEN ? = 'sent' THEN ? END, ?)
        """,
        (f"{LIST_URL}{raw_job_id}/", status, error, status, at, at),
    )


def add_call(conn: sqlite3.Connection, model: str, input_tokens: int, output_tokens: int) -> None:
    record_call(
        conn,
        feature=CLASSIFY,
        usage=Usage(
            provider="gemini",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            latency_ms=100,
        ),
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


def near(body: str, label: str, size: int = 300) -> str:
    return body[body.index(label) : body.index(label) + size]


def test_오늘_새_공고_건수가_수집_시각과_같다(client: TestClient, conn: sqlite3.Connection) -> None:
    add_raw(conn, 1, TODAY)
    add_raw(conn, 2, TODAY)
    add_raw(conn, 3, YESTERDAY)
    conn.commit()

    part = near(client.get("/ui/dashboard").text, "오늘 새 공고")

    assert ">2<" in part
    assert "어제 1건" in part


def test_오늘_오공고_전송은_보낸_시각으로_세고_실패는_세지_않는다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    for raw_job_id in (1, 2, 3, 4):
        add_raw(conn, raw_job_id, YESTERDAY)
        add_job(conn, raw_job_id)
    add_delivery(conn, 1, status="sent", at=TODAY)
    add_delivery(conn, 2, status="sent", at=TODAY)
    add_delivery(conn, 3, status="sent", at=YESTERDAY)
    add_delivery(conn, 4, status="failed", at=TODAY, error="400 제목이 너무 길다")
    conn.commit()

    part = near(client.get("/ui/dashboard").text, "오늘 오공고 전송")

    assert ">2<" in part
    assert "어제 1건" in part


def test_처리_대기는_분류_대기와_전송_대기를_따로_센다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    add_raw(conn, 1, TODAY, body="분류 전 본문")  # 분류 대기
    add_raw(conn, 2, TODAY, body="분류한 본문")
    add_job(conn, 2)  # 필수 칸이 차 있고 아직 안 보냈다 → 전송 대기
    conn.commit()

    body = client.get("/ui/dashboard").text

    assert "분류 대기 1 · 전송 대기 1" in body


def test_비용은_모델별_단가로_계산된다(client: TestClient, conn: sqlite3.Connection) -> None:
    add_call(conn, "gemini-3.5-flash", 1_000_000, 1_000_000)  # $1.50 + $9.00
    conn.commit()

    part = near(client.get("/ui/dashboard").text, "오늘 AI 비용")

    assert f"{round(10.5 * pricing.KRW_PER_USD):,}" in part
    assert "$10.50" in part
    assert "호출 1회" in part


def test_단가_없는_모델의_호출은_비용에서_빠지고_따로_적힌다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    add_call(conn, "gpt-oss:120b", 1_000_000, 1_000_000)
    conn.commit()

    part = near(client.get("/ui/dashboard").text, "오늘 AI 비용")

    assert "$0.00" in part
    assert "단가 없는 호출 1회" in part


def test_단가표는_모르는_모델에_None_이다() -> None:
    assert pricing.cost_usd("gemini-3.5-flash", 1_000_000, 0) == pytest.approx(1.5)
    assert pricing.cost_usd("모르는-모델", 1_000_000, 0) is None


def test_연속_실패한_워크플로우를_알린다(client: TestClient, conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO crawl_runs (workflow_id, started_at, status, error_message)"
        " VALUES (1, ?, 'failed', '목록 셀렉터가 0건을 뽑았다')",
        (TODAY,),
    )
    conn.commit()

    part = near(client.get("/ui/dashboard").text, "확인이 필요한 것", 800)

    assert "lg 워크플로우" in part
    assert "연속 실패 1" in part
    assert "목록 셀렉터가 0건을 뽑았다" in part


def test_오공고_전송_실패를_알린다(client: TestClient, conn: sqlite3.Connection) -> None:
    add_raw(conn, 1, TODAY)
    add_job(conn, 1)
    add_delivery(conn, 1, status="failed", at=TODAY, error="400 제목이 너무 길다")
    conn.commit()

    part = near(client.get("/ui/dashboard").text, "확인이 필요한 것", 800)

    assert "오공고 전송 실패" in part
    assert "400 제목이 너무 길다" in part


def test_분류는_끝났는데_필수_칸이_빈_공고를_알린다(
    client: TestClient, conn: sqlite3.Connection
) -> None:
    add_raw(conn, 1, TODAY)
    add_job(conn, 1, education_level=None)
    add_raw(conn, 2, TODAY)
    add_job(conn, 2, education_level=None, classified=False)  # 아직 분류 전이면 세지 않는다
    conn.commit()

    part = near(client.get("/ui/dashboard").text, "확인이 필요한 것", 800)

    assert "필수 칸이 빈 공고" in part
    assert "1건" in part


def test_확인할_것이_없으면_그렇게_적는다(client: TestClient) -> None:
    assert "지금 확인할 것이 없다" in client.get("/ui/dashboard").text


def test_최근_전송한_공고가_나온다(client: TestClient, conn: sqlite3.Connection) -> None:
    add_raw(conn, 1, TODAY)
    add_job(conn, 1)
    add_delivery(conn, 1, status="sent", at=TODAY)
    conn.commit()

    part = near(client.get("/ui/dashboard").text, "최근 전송한 공고", 1200)

    assert "공고 1" in part


def test_보낸_공고가_없으면_안내를_적는다(client: TestClient) -> None:
    assert "아직 오공고로 보낸 공고가 없다" in client.get("/ui/dashboard").text


def test_워크플로우_상태가_나온다(client: TestClient, conn: sqlite3.Connection) -> None:
    body = client.get("/ui/dashboard").text
    assert "기록 없음" in near(body, "관리</a>", 600)

    conn.execute(
        "INSERT INTO crawl_runs (workflow_id, started_at, status) VALUES (1, ?, 'success')",
        (TODAY,),
    )
    conn.commit()

    assert "정상" in near(client.get("/ui/dashboard").text, "관리</a>", 600)


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
