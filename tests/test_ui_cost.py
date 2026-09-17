"""비용 화면과 모델 단가 (2026-09-17, LC-3344).

AI 를 부르지 않는다. 호출 기록(`llm_calls`)을 넣고 화면이 어떻게 세는지만 본다.

| 확인 | 깨지면 |
|---|---|
| 기간 안의 호출만 세고 기능·모델로 나눈다 | 지난달 비용이 이번 달에 섞인다 |
| 단가를 모르는 모델은 비용에 넣지 않고 알린다 | 0원으로 더해져 비용이 적게 보인다 |
| 화면에서 넣은 단가가 코드 표를 이긴다 | 새 모델마다 배포해야 한다 |
| 공고 1건당 평균은 그 기간에 분류한 공고로 나눈다 | 호출 수로 나눠 엉뚱한 값이 된다 |
"""

from __future__ import annotations

import pathlib
import re
import sqlite3
from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import crawlers as crawlers_api
from app.api.ui_cost import bounds, build_report
from app.llm import pricing
from app.main import app

TODAY = date(2026, 9, 17)


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


def call(
    conn: sqlite3.Connection,
    model: str,
    feature: str,
    called_at: str,
    *,
    tokens: tuple[int, int] = (1_000_000, 0),
    ok: int = 1,
) -> None:
    conn.execute(
        "INSERT INTO llm_calls (provider, model, feature, input_tokens, output_tokens, ok,"
        " latency_ms, called_at) VALUES ('p', ?, ?, ?, ?, ?, 2000, ?)",
        (model, feature, tokens[0], tokens[1], ok, called_at),
    )


def test_기간은_표시_시간대의_날짜로_자른다() -> None:
    assert bounds("today", TODAY) == (TODAY, date(2026, 9, 18))
    assert bounds("7d", TODAY) == (date(2026, 9, 11), date(2026, 9, 18))
    assert bounds("month", TODAY) == (date(2026, 9, 1), date(2026, 9, 18))
    assert bounds("last_month", TODAY) == (date(2026, 8, 1), date(2026, 9, 1))


def test_기간_안의_호출만_세고_기능과_모델로_나눈다(conn: sqlite3.Connection) -> None:
    # gemini-3.5-flash 입력 100만 토큰 = 1.5달러
    call(conn, "gemini-3.5-flash", "classify", "2026-09-10 03:00:00")
    call(conn, "gemini-3.5-flash", "selector_generate", "2026-09-16 03:00:00", ok=0)
    call(conn, "gemini-3.5-flash", "classify", "2026-08-20 03:00:00")  # 지난달

    report = build_report(conn, "month", TODAY)

    assert report.total.calls == 2
    assert report.total.failed == 1
    assert report.total.usd == pytest.approx(3.0)
    assert dict((name, bucket.calls) for name, bucket in report.features) == {
        "공고 분류": 1,
        "셀렉터 생성·수정": 1,
    }
    assert len(report.days) == 17
    assert report.previous_usd == pytest.approx(1.5)
    assert report.change_pct == 100


def test_단가를_모르는_모델은_비용에_넣지_않고_알린다(conn: sqlite3.Connection) -> None:
    call(conn, "mystery-model", "classify", "2026-09-10 03:00:00")

    report = build_report(conn, "month", TODAY)

    assert report.total.usd == 0
    assert report.unpriced_models == [("mystery-model", 1)]


def test_화면에서_넣은_단가가_코드_표를_이긴다(conn: sqlite3.Connection) -> None:
    call(conn, "gemini-3.5-flash", "classify", "2026-09-10 03:00:00")
    call(conn, "deepseek-flash", "classify", "2026-09-10 03:00:00")
    pricing.save_price(conn, "gemini-3.5-flash", 1.0, 0.0)
    pricing.save_price(conn, "deepseek-flash", 0.15, 0.6)

    report = build_report(conn, "month", TODAY)

    assert report.total.usd == pytest.approx(1.15)
    assert pricing.price_table(conn)["deepseek-flash"].source == pricing.SOURCE_STORED
    pricing.delete_price(conn, "gemini-3.5-flash")
    assert pricing.price_table(conn)["gemini-3.5-flash"].source == pricing.SOURCE_CODE


def test_공고_1건당_평균은_그_기간에_분류한_공고로_나눈다(conn: sqlite3.Connection) -> None:
    call(conn, "gemini-3.5-flash", "classify", "2026-09-10 03:00:00")
    conn.execute("INSERT INTO crawlers (id, name, list_url) VALUES (1, 'x', 'https://x')")
    conn.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, 'x')")
    for seq in (1, 2, 3):
        conn.execute(
            "INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)"
            " VALUES (?, 1, ?, '{}', ?)",
            (seq, f"https://x/{seq}", f"h{seq}"),
        )
        conn.execute(
            "INSERT INTO job_classifications (raw_job_id, model, classified_at)"
            " VALUES (?, 'm', '2026-09-10 04:00:00')",
            (seq,),
        )

    report = build_report(conn, "month", TODAY)

    assert report.classified == 3
    assert report.per_job_krw == round(1.5 * pricing.KRW_PER_USD / 3, 1)


def test_비용_화면이_열리고_기간을_고른다(client: TestClient) -> None:
    body = client.get("/cost?period=7d").text

    assert "<title>비용" in body
    assert 'href="/cost?period=7d" aria-current="page"' in body
    assert "기능별" in body and "모델별" in body


def test_설정_AI에서_단가를_넣고_지운다(client: TestClient, conn: sqlite3.Connection) -> None:
    assert 'hx-get="/ui/prices"' in client.get("/settings").text

    saved = client.post(
        "/ui/prices", data={"model": "deepseek-flash", "input_usd": "0.15", "output_usd": "0.6"}
    ).text
    assert "deepseek-flash 단가를 저장했다" in saved
    assert re.search(r"deepseek-flash.*?직접 입력", saved, re.S)

    bad = client.post("/ui/prices", data={"model": "x", "input_usd": "-1", "output_usd": "1"}).text
    assert "0 이상의 숫자" in bad

    removed = client.post("/ui/prices", data={"model": "deepseek-flash"}).text
    assert "직접 입력 단가를 지웠다" in removed
