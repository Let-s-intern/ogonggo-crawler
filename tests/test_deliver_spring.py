"""정규화한 공고를 오공고(Spring)에 등록한다 (2026-09-15 결정).

오공고를 실제로 부르지 않는다. `httpx.MockTransport` 가 오공고 관리자 API 인 척 답한다.

| 확인 | 깨지면 |
|---|---|
| 정규화 행을 오공고 요청 칸으로 옮긴다 | 오공고가 400 으로 거절하거나 값이 엉뚱한 칸에 들어간다 |
| 새 공고를 등록하고 받은 id 를 적고, 두 번 보내지 않는다 | 분류 때마다 같은 공고가 쌓인다 |
| 409 면 원문 주소로 id 를 찾아 보낸 것으로 적는다 | id 를 잃은 공고를 영영 못 보낸다 |
| 거절되면 사유를 남기고 세 번까지만 다시 보낸다 | 같은 거절을 끝없이 되풀이한다 |
| 필수 칸이 빈 공고와 마감이 지난 공고는 보내지 않는다 | 끝난 공고가 검수 목록에 쌓인다 |
| 꺼져 있거나 주소·키가 없으면 보내지 않고, 지금 보내기는 꺼져 있어도 보낸다 | 설정 전에 보낸다 |
| 분류 배치가 끝나면 보낸다 | 켜 두어도 아무것도 가지 않는다 |
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from app import db
from app.classify.batch import ClassifyProgress, classify_pending
from app.config import Settings
from app.deliver import spring
from app.deliver.settings import DeliverConfig, write_config
from tests.classify_fakes import response
from tests.test_classify_run import _seed
from tests.test_selector_generator import FakeClient

KEY = "test-internal-key"
SETTINGS = Settings(gemini_api_key="테스트키", ogonggo_internal_api_key=KEY)
FUTURE = "2099-12-31 23:59:59"


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    connection.execute("INSERT INTO crawlers (id, name, list_url) VALUES (1, '예시', 'https://x')")
    connection.execute("INSERT INTO workflows (id, crawler_id, name) VALUES (1, 1, '예시')")
    write_config(connection, DeliverConfig(url="https://ogonggo.test/", enabled=True))
    try:
        yield connection
    finally:
        connection.close()


def add_job(conn: sqlite3.Connection, seq: int, **values: Any) -> str:
    """자동 전송할 수 있는 정규화 행 하나. 기본 정보·모집 조건이 다 차 있다. 원문 주소를 준다."""
    source_url = f"https://x/{seq}"
    conn.execute(
        "INSERT INTO raw_jobs (id, workflow_id, source_url, raw_data_json, content_hash)"
        " VALUES (?, 1, ?, '{}', ?)",
        (seq, source_url, f"hash-{seq}"),
    )
    row = {
        "raw_job_id": seq,
        "source_url": source_url,
        "company_name": "예시",
        "title": f"공고 {seq}",
        "employment_type": "FULL_TIME",
        "experience_type": "EXPERIENCED",
        "education_level": "BACHELOR",
        "recruitment_type": "PERIOD",
        "recruitment_end_at": FUTURE,
        "industry": "IT·정보통신업",
        "job_field": "IT·개발",
        "job_role": "서버·백엔드",
        "cover_image_url": "https://x/logo.png",
        "region": "서울",
        "application_method": "EXTERNAL_PAGE",
        "recruitment_start_at": "2026-09-01 00:00:00",
        "closes_when_filled": "false",
        "auto_close_enabled": "true",
        **values,
    }
    conn.execute(
        f"INSERT INTO normalized_jobs ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
        tuple(row.values()),
    )
    return source_url


def mock(
    handle: Callable[[httpx.Request], httpx.Response], monkeypatch: pytest.MonkeyPatch
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handle(request)

    monkeypatch.setattr(spring, "transport", httpx.MockTransport(record))
    return seen


def created(job_id: int) -> httpx.Response:
    return httpx.Response(
        201, json={"status": 201, "message": "요청이 성공했습니다.", "data": {"jobId": job_id}}
    )


def delivery(conn: sqlite3.Connection, source_url: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM spring_deliveries WHERE source_url = ?", (source_url,)
    ).fetchone()


def job_row(**values: Any) -> dict[str, Any]:
    columns = (
        "company_name parent_company_name title job_field job_role industry cover_image_url"
        " employment_type experience_type experience_min_years education_level region"
        " recruitment_type recruitment_headcount recruitment_start_at recruitment_end_at"
        " closes_when_filled auto_close_enabled company_and_team_introduction responsibilities"
        " qualifications preferred_qualifications compensation benefits hiring_process"
        " recruitment_notice application_method source_url"
    ).split()
    return {**dict.fromkeys(columns), **values}


def test_정규화_행을_오공고_요청_칸으로_옮긴다() -> None:
    body = spring.payload(
        job_row(
            company_name="삼성전기",
            parent_company_name="삼성",
            title="회로 설계",
            employment_type="FULL_TIME",
            experience_type="EXPERIENCED",
            experience_min_years="3",
            education_level="BACHELOR",
            recruitment_type="PERIOD",
            recruitment_headcount="2",
            recruitment_start_at="2026-09-01 00:00:00",
            recruitment_end_at="2026-09-30 23:59:59",
            closes_when_filled="true",
            auto_close_enabled="false",
            responsibilities="회로를 설계합니다",
            benefits="  ",
            application_method="EMAIL",
            source_url="https://x/1#2",
        )
    )

    assert body["companyName"] == "삼성전기"
    assert body["parentCompanyName"] == "삼성"
    assert (body["experienceMinYears"], body["recruitmentHeadcount"]) == (3, 2)
    assert body["recruitmentStartAt"] == "2026-09-01T00:00:00"
    assert body["recruitmentEndAt"] == "2026-09-30T23:59:59"
    assert (body["closesWhenFilled"], body["autoCloseEnabled"]) == (True, False)
    assert body["benefits"] is None
    assert body["applicationMethod"] == "EMAIL"
    assert body["sourceUrl"] == "https://x/1#2"
    assert "body" not in body
    assert spring.missing(body) == []


def test_회사명이_모회사뿐이면_모회사가_회사명이고_상시채용은_마감일이_없다() -> None:
    body = spring.payload(
        job_row(
            parent_company_name="토스",
            title="백엔드",
            employment_type="정규직",
            recruitment_type="ALWAYS_OPEN",
            recruitment_end_at="2026-09-30 23:59:59",
        )
    )

    assert (body["companyName"], body["parentCompanyName"]) == ("토스", None)
    assert body["recruitmentEndAt"] is None
    # 목록 밖 옛 한글 값은 비어 있는 것으로 본다
    assert body["employmentType"] is None
    assert "employmentType" in spring.missing(body)


async def test_새_공고를_등록하고_받은_id를_적고_두_번_보내지_않는다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_url = add_job(conn, 1)
    seen = mock(lambda request: created(42), monkeypatch)

    first = await spring.deliver_pending(conn, settings=SETTINGS)
    second = await spring.deliver_pending(conn, settings=SETTINGS)

    assert (first.sent, first.failed, second.sent) == (1, 0, 0)
    assert len(seen) == 1
    assert seen[0].method == "POST"
    assert str(seen[0].url) == "https://ogonggo.test/api/v1/internal/jobs"
    assert seen[0].headers[spring.API_KEY_HEADER] == KEY
    assert json.loads(seen[0].content)["sourceUrl"] == source_url
    row = delivery(conn, source_url)
    assert (row["status"], row["spring_job_id"]) == ("sent", 42)


async def test_409면_원문_주소로_id를_찾아_보낸_것으로_적는다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_url = add_job(conn, 1)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(409, json={"status": 409, "code": "C", "message": "이미 있다"})
        return httpx.Response(200, json={"status": 200, "message": "성공", "data": {"jobId": 7}})

    seen = mock(handle, monkeypatch)

    result = await spring.deliver_pending(conn, settings=SETTINGS)

    assert result.sent == 1
    assert seen[1].method == "GET"
    assert seen[1].url.params["sourceUrl"] == source_url
    assert delivery(conn, source_url)["spring_job_id"] == 7


async def test_거절되면_사유를_남기고_세_번까지만_다시_보낸다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_url = add_job(conn, 1)
    seen = mock(
        lambda request: httpx.Response(
            400, json={"status": 400, "code": "C", "message": "자회사명은 150자 이하여야 합니다."}
        ),
        monkeypatch,
    )

    for _ in range(spring.MAX_ATTEMPTS + 1):
        await spring.deliver_pending(conn, settings=SETTINGS)

    assert len(seen) == spring.MAX_ATTEMPTS
    row = delivery(conn, source_url)
    assert (row["status"], row["attempts"]) == ("failed", spring.MAX_ATTEMPTS)
    assert row["last_error"] == "400 자회사명은 150자 이하여야 합니다."
    assert spring.overview(conn).failures[0].source_url == source_url


async def test_필수_칸이_빈_공고와_마감이_지난_공고는_보내지_않는다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_job(conn, 1, employment_type=None)
    add_job(conn, 2, recruitment_end_at="2000-01-01 23:59:59")
    always_open = add_job(conn, 3, recruitment_type="ALWAYS_OPEN", recruitment_end_at=None)
    seen = mock(lambda request: created(3), monkeypatch)

    result = await spring.deliver_pending(conn, settings=SETTINGS)

    assert result.sent == 1
    assert [json.loads(request.content)["sourceUrl"] for request in seen] == [always_open]


async def test_꺼져_있거나_키가_없으면_보내지_않고_지금_보내기는_보낸다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_job(conn, 1)
    seen = mock(lambda request: created(1), monkeypatch)
    no_key = Settings(gemini_api_key="테스트키", ogonggo_internal_api_key="")

    missing_key = await spring.deliver_pending(conn, settings=no_key)
    write_config(conn, DeliverConfig(url="https://ogonggo.test", enabled=False))
    turned_off = await spring.deliver_pending(conn, settings=SETTINGS)
    forced = await spring.deliver_pending(conn, settings=SETTINGS, force=True)

    assert "OGONGGO_INTERNAL_API_KEY" in missing_key.reason
    assert "꺼져" in turned_off.reason
    assert (forced.reason, forced.sent) == ("", 1)
    assert len(seen) == 1


async def test_자동_전송은_기본_정보와_모집_조건이_다_찬_공고만_보낸다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-09-18 결정. 최소 경력 연수·모집 인원·모회사는 비어도 되고, 상시 채용은 마감일이 없다."""
    add_job(conn, 1, industry=None)
    add_job(conn, 2, region="")
    optional_empty = add_job(conn, 3, experience_min_years=None, recruitment_headcount=None)
    always_open = add_job(conn, 4, recruitment_type="ALWAYS_OPEN", recruitment_end_at=None)
    seen = mock(lambda request: created(1), monkeypatch)

    result = await spring.deliver_pending(conn, settings=SETTINGS)

    assert result.sent == 2
    assert [json.loads(request.content)["sourceUrl"] for request in seen] == [
        optional_empty,
        always_open,
    ]


async def test_지금_보내기는_필수_칸만_본다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_job(conn, 1, industry=None, region=None)
    mock(lambda request: created(1), monkeypatch)

    automatic = await spring.deliver_pending(conn, settings=SETTINGS)
    forced = await spring.deliver_pending(conn, settings=SETTINGS, force=True)

    assert (automatic.sent, forced.sent) == (0, 1)


def test_자동_전송을_막는_빈_칸을_가린다() -> None:
    full = job_row(**{**{name: "값" for name in spring.AUTO_FIELDS}, "recruitment_type": "PERIOD"})
    assert spring.auto_missing(full) == []
    assert spring.auto_missing({**full, "industry": None, "region": " "}) == ["industry", "region"]
    # 회사는 모회사만 있어도 된다. 상시 채용은 마감일이 없어도 된다
    assert (
        spring.auto_missing({**full, "company_name": None, "parent_company_name": "모회사"}) == []
    )
    assert (
        spring.auto_missing({**full, "recruitment_type": "ALWAYS_OPEN", "recruitment_end_at": None})
        == []
    )
    assert spring.auto_missing({**full, "recruitment_end_at": None}) == ["recruitment_end_at"]


async def test_분류_배치가_끝나면_칸이_빈_공고는_자동으로_보내지_않는다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """산업·직군·근무 지역 같은 칸이 빈 공고다. 필수 칸이 다 찼어도 자동으로는 보내지 않는다."""
    conn.execute("DELETE FROM workflows")
    conn.execute("DELETE FROM crawlers")
    _seed(conn, count=1)
    seen = mock(lambda request: created(9), monkeypatch)
    text = response(
        responsibilities="제휴사 데이터 연동 구조 기획",
        employment_type="FULL_TIME",
        experience_type="EXPERIENCED",
        education_level="BACHELOR",
        closes_when_filled="false",
        application_method="EXTERNAL_PAGE",
    )

    await classify_pending(conn, ClassifyProgress(), client=FakeClient(text), settings=SETTINGS)

    assert seen == []


async def test_분류_배치가_끝나면_보낸다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn.execute("DELETE FROM workflows")
    conn.execute("DELETE FROM crawlers")
    _seed(conn, count=1)
    # 분류 배치 끝의 전송 경로만 본다. 칸이 다 찼는지는 위 테스트들이 본다
    monkeypatch.setattr(spring, "COMPLETE_SQL", spring.READY_SQL)
    seen = mock(lambda request: created(9), monkeypatch)
    text = response(
        responsibilities="제휴사 데이터 연동 구조 기획",
        employment_type="FULL_TIME",
        experience_type="EXPERIENCED",
        education_level="BACHELOR",
        closes_when_filled="false",
        application_method="EXTERNAL_PAGE",
    )

    await classify_pending(conn, ClassifyProgress(), client=FakeClient(text), settings=SETTINGS)

    assert len(seen) == 1
    body = json.loads(seen[0].content)
    assert (body["companyName"], body["recruitmentType"]) == ("테스트회사", "ALWAYS_OPEN")
    assert delivery(conn, body["sourceUrl"])["spring_job_id"] == 9
