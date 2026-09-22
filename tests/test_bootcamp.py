"""새싹(SeSAC) 부트캠프 수집·정리·전송 (2026-09-22 결정, LC-3364).

새싹과 오공고를 실제로 부르지 않는다. 새싹은 저장해 둔 페이지(`tests/fixtures/sesac-*`)를
돌려주는 가짜 fetcher 가, 오공고는 `httpx.MockTransport` 가 흉내 낸다.

| 확인 | 깨지면 |
|---|---|
| 목록·상세의 날짜·캠퍼스·분야·목차·교육개요 이미지를 읽는다 | 오공고 필수 칸이 비어 거절된다 |
| 이미 모아 정리한 과정은 상세도 AI 도 다시 부르지 않는다 | 수집마다 같은 과정에 비용이 든다 |
| 목록이 비었는데 총 건수가 0 이 아니면 실패다 | 새싹 틀이 바뀐 날이 성공으로 보인다 |
| 등록하고 id 를 적고, 두 번 보내지 않는다 | 같은 과정이 오공고에 쌓인다 |
| 409 면 원문 주소로 id 를 찾아 교체한다 | id 를 잃은 과정을 영영 못 보낸다 |
| 목차를 교육 시작일부터 센 주차로 바꾸고, 많으면 주차별로 합친다 | 목차가 수업 목록이 된다 |
| 상태와 상관없이 모으고, 끝난 과정은 모집 마감으로 보낸다 | 끝난 과정이 모집중으로 보인다 |
| 모은 과정의 상태가 바뀌면 상세 없이 다시 보낸다 | 개강한 과정이 모집중으로 남는다 |
| 한 번에 정리하는 수에 상한을 두고 모집중부터 채운다 | 첫 수집이 한 시간 넘게 돈다 |
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Iterator
from datetime import date
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app import db
from app.api import settings as settings_api
from app.bootcamp import deliver, schedule, sesac, store
from app.bootcamp import settings as bootcamp_settings
from app.bootcamp.fill import BootcampFill, BootcampFillError, LlmBootcampFiller
from app.bootcamp.runner import run_sesac
from app.config import Settings
from app.crawler.fetcher import FetchResult
from app.deliver.settings import DeliverConfig, write_config
from app.main import app
from app.scheduler import get_scheduler
from tests.test_selector_generator import FakeClient as FakeGeminiClient

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
LIST_HTML = (FIXTURES / "sesac-list-recruiting-20260922.html").read_text(encoding="utf-8")
DETAILS = {
    sn: (FIXTURES / f"sesac-detail-{sn}-20260922.html").read_text(encoding="utf-8")
    for sn in ("1197", "1189", "1154", "1202")
}
ALL_LIST_HTML = (FIXTURES / "sesac-list-all-p1-20260922.html").read_text(encoding="utf-8")
KEY = "test-internal-key"
SETTINGS = Settings(gemini_api_key="테스트키", ogonggo_internal_api_key=KEY)
EMPTY_LIST = LIST_HTML.split('<div class="list-wrap v1">')[0] + "</body></html>"


class SesacFetcher:
    """목록 첫 쪽과 상세 세 개를 돌려준다.

    둘째 쪽부터는 첫 쪽과 같다 — 새싹이 실제로 그렇게 답한다.
    """

    def __init__(self, list_html: str = LIST_HTML, details: dict[str, str] | None = None) -> None:
        self.list_html = list_html
        self.details = dict(details or DETAILS)
        self.urls: list[str] = []

    async def fetch(self, url: str) -> FetchResult:
        self.urls.append(url)
        if sesac.LIST_PATH in url:
            return FetchResult(url=url, status_code=200, text=self.list_html)
        crs_sn = url.rsplit("crsSn=", 1)[1]
        return FetchResult(url=url, status_code=200, text=self.details[crs_sn])

    async def request(self, url: str, **_: Any) -> FetchResult:
        return await self.fetch(url)


class FakeFiller:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.titles: list[str] = []

    async def fill(self, course: sesac.Course) -> BootcampFill:
        self.titles.append(course.title)
        if self.error is not None:
            raise self.error
        return BootcampFill(
            short_description=f"{course.campus} 한 줄 소개",
            content="■ 과정 소개\n- 실무 프로젝트",
            eligibility_and_selection_process="만 34세 이하 서울 청년",
            capacity=30,
        )


@pytest.fixture
def conn(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "jobs.db")
    db.migrate_up(connection)
    write_config(connection, DeliverConfig(url="https://ogonggo.test/", enabled=False))
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture(autouse=True)
def no_transport() -> Iterator[None]:
    yield
    deliver.transport = None


def test_목록에서_모집_중_과정과_썸네일을_읽는다() -> None:
    items = sesac.parse_list(LIST_HTML)

    assert [item.crs_sn for item in items] == ["1197", "1189", "1154"]
    assert items[0].thumbnail_url.startswith("https://sesac.seoul.kr/uploadData/course/")
    assert sesac.total_count(LIST_HTML) == 3


def test_상세에서_날짜_캠퍼스_분야_목차_이미지를_읽는다() -> None:
    course = sesac.parse_detail(DETAILS["1197"], "1197")

    assert (
        course.source_url
        == "https://sesac.seoul.kr/sesac/course/offline/courseDetail.do?crsSn=1197"
    )
    assert course.title.startswith("(성동4기) 데이터 수집부터")
    assert (course.status, course.campus, course.category) == ("모집중", "성동", "클라우드")
    assert (course.recruitment_start, course.recruitment_end) == (
        date(2026, 8, 13),
        date(2026, 9, 28),
    )
    assert (course.program_start, course.program_end) == (date(2026, 10, 12), date(2027, 3, 3))
    assert course.hours == 776
    assert len(course.overview_images) == 1
    assert [group.name for group in course.curriculum][:2] == ["1차시 IT 이해", "2차시 Database"]
    assert course.curriculum[0].first_day == date(2026, 10, 12)
    assert len(course.curriculum[0].lessons) == 5


def test_교육개요가_이미지뿐인_과정도_읽는다() -> None:
    course = sesac.parse_detail(DETAILS["1189"], "1189")

    assert course.overview_text == ""
    assert len(course.overview_images) == 13


def test_틀이_바뀌어_과정명이_없으면_실패다() -> None:
    with pytest.raises(sesac.SesacParseError):
        sesac.parse_detail("<html><body></body></html>", "1")


def test_목차를_교육_시작일부터_센_주차로_바꾼다() -> None:
    course = sesac.parse_detail(DETAILS["1197"], "1197")

    items = deliver.curriculums(list(course.curriculum), date(2026, 10, 12))

    assert items[0] == {"startWeek": 1, "endWeek": 1, "subtitle": "IT 이해"}
    assert items[-1]["subtitle"] == "최종 프로젝트"
    assert items[-1]["endWeek"] == 21
    assert len(items) == 7


def test_차시가_많으면_같은_주에_시작하는_묶음을_합친다() -> None:
    course = sesac.parse_detail(DETAILS["1154"], "1154")
    assert len(course.curriculum) == 48

    items = deliver.curriculums(list(course.curriculum), date(2026, 11, 4))

    assert len(items) <= deliver.MAX_CURRICULUM_GROUPS + 2
    assert [item["startWeek"] for item in items] == sorted({item["startWeek"] for item in items})
    assert all(len(item["subtitle"]) <= deliver.SUBTITLE_LIMIT for item in items)


async def test_새_과정만_AI_로_채우고_같은_과정은_다시_채우지_않는다(
    conn: sqlite3.Connection,
) -> None:
    filler = FakeFiller()

    first = await run_sesac(conn, fetcher=SesacFetcher(), filler=filler, settings=SETTINGS)
    second = await run_sesac(conn, fetcher=SesacFetcher(), filler=filler, settings=SETTINGS)

    assert (first.status, first.listed, first.new, first.filled) == ("success", 3, 3, 3)
    assert (second.status, second.new, second.skipped, second.filled) == ("success", 0, 3, 0)
    assert len(filler.titles) == 3
    runs = conn.execute("SELECT status, new_count FROM bootcamp_runs ORDER BY id").fetchall()
    assert [tuple(run) for run in runs] == [("success", 3), ("success", 0)]


async def test_이미_모은_과정은_안내가_바뀌어도_상세를_다시_받지_않는다(
    conn: sqlite3.Connection,
) -> None:
    await run_sesac(conn, fetcher=SesacFetcher(), filler=FakeFiller(), settings=SETTINGS)
    changed = DETAILS["1197"].replace("2026.08.13 - 2026.09.28", "2026.08.13 - 2026.10.05")
    fetcher = SesacFetcher(details={**DETAILS, "1197": changed})
    filler = FakeFiller()

    summary = await run_sesac(conn, fetcher=fetcher, filler=filler, settings=SETTINGS)

    assert (summary.new, summary.skipped, summary.filled) == (0, 3, 0)
    assert all(sesac.LIST_PATH in url for url in fetcher.urls)
    assert filler.titles == []
    row = conn.execute("SELECT * FROM bootcamps WHERE external_id = '1197'").fetchone()
    assert row["recruitment_end_date"] == "2026-09-28"


async def test_AI_가_실패하면_사유를_남기고_다음_수집에서_다시_채운다(
    conn: sqlite3.Connection,
) -> None:
    await run_sesac(
        conn,
        fetcher=SesacFetcher(),
        filler=FakeFiller(error=BootcampFillError("AI 호출이 실패했다")),
        settings=SETTINGS,
    )
    assert conn.execute("SELECT count(*) FROM bootcamps WHERE fill_error <> ''").fetchone()[0] == 3

    retry = await run_sesac(conn, fetcher=SesacFetcher(), filler=FakeFiller(), settings=SETTINGS)

    assert retry.filled == 3
    assert conn.execute("SELECT count(*) FROM bootcamps WHERE fill_error <> ''").fetchone()[0] == 0


async def test_목록이_비었는데_총_건수가_있으면_실패다(conn: sqlite3.Connection) -> None:
    assert sesac.total_count(EMPTY_LIST) == 3

    summary = await run_sesac(
        conn, fetcher=SesacFetcher(list_html=EMPTY_LIST), filler=FakeFiller(), settings=SETTINGS
    )

    assert summary.status == "failed"
    assert "하나도 읽지 못했다" in summary.notes[0]


def _spring(handler: Any) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    deliver.transport = httpx.MockTransport(wrapped)
    return seen


async def test_등록하고_id_를_적고_두_번_보내지_않는다(conn: sqlite3.Connection) -> None:
    await run_sesac(conn, fetcher=SesacFetcher(), filler=FakeFiller(), settings=SETTINGS)
    ids = iter(range(101, 200))
    seen = _spring(
        lambda request: httpx.Response(
            201 if request.method == "POST" else 200,
            json={"data": {"bootcampId": next(ids)} if request.method == "POST" else None},
        )
    )

    first = await deliver.deliver_pending(conn, settings=SETTINGS)
    again = await deliver.deliver_pending(conn, settings=SETTINGS)

    assert (first.sent, first.failed, again.sent) == (3, 0, 0)
    assert [request.method for request in seen] == ["POST", "POST", "POST"]
    assert seen[0].headers["X-Internal-Api-Key"] == KEY
    body = json.loads(seen[0].content)
    assert body["companyName"] == "새싹 성동캠퍼스"
    assert body["operationType"] == "OFFLINE"
    assert body["recruitmentType"] == "PERIOD"
    assert body["recruitmentStartAt"] == "2026-08-13T00:00:00"
    assert body["recruitmentEndAt"] == "2026-09-28T23:59:59"
    assert body["programStartDate"] == "2026-10-12"
    assert body["tuitionType"] == "FREE"
    assert body["applicationMethod"] == "EXTERNAL_PAGE"
    assert body["applicationUrl"] == body["sourceUrl"]
    assert body["representativeImageUrl"].startswith("https://sesac.seoul.kr/")
    assert body["capacity"] == 30
    assert body["curriculums"][0]["subtitle"] == "IT 이해"


async def test_409_면_원문_주소로_id_를_찾아_교체한다(conn: sqlite3.Connection) -> None:
    await run_sesac(conn, fetcher=SesacFetcher(), filler=FakeFiller(), settings=SETTINGS)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(409, json={"message": "이미 등록된 원문 URL입니다."})
        if request.method == "GET":
            return httpx.Response(200, json={"data": {"bootcampId": 7}})
        return httpx.Response(200, json={"data": None})

    seen = _spring(handler)

    result = await deliver.deliver_pending(conn, settings=SETTINGS)

    assert result.sent == 3
    assert [request.method for request in seen[:3]] == ["POST", "GET", "PUT"]
    assert seen[1].url.params["sourceUrl"].startswith("https://sesac.seoul.kr/")
    assert {row[0] for row in conn.execute("SELECT spring_bootcamp_id FROM bootcamps")} == {7}


async def test_거절되면_사유를_남기고_세_번까지만_자동으로_보낸다(conn: sqlite3.Connection) -> None:
    await run_sesac(conn, fetcher=SesacFetcher(), filler=FakeFiller(), settings=SETTINGS)
    _spring(lambda _: httpx.Response(400, json={"message": "[title] 과정명을 입력해 주세요."}))

    for _ in range(deliver.MAX_ATTEMPTS):
        await deliver.deliver_pending(conn, settings=SETTINGS)

    row = conn.execute("SELECT * FROM bootcamps ORDER BY id LIMIT 1").fetchone()
    assert (row["send_status"], row["send_attempts"]) == ("failed", deliver.MAX_ATTEMPTS)
    assert "과정명" in row["send_error"]
    assert deliver.pending(conn) == []
    assert len(deliver.pending(conn, retry_failed=True)) == 3


async def test_주소나_키가_없으면_보내지_않는다(conn: sqlite3.Connection) -> None:
    result = await deliver.deliver_pending(conn, settings=Settings(ogonggo_internal_api_key=""))

    assert "OGONGGO_INTERNAL_API_KEY" in result.reason


async def test_전송을_켜_두면_수집이_끝날_때_보낸다(conn: sqlite3.Connection) -> None:
    bootcamp_settings.write_config(conn, bootcamp_settings.BootcampConfig(deliver_enabled=True))
    ids = iter(range(1, 10))
    _spring(lambda _: httpx.Response(201, json={"data": {"bootcampId": next(ids)}}))

    summary = await run_sesac(conn, fetcher=SesacFetcher(), filler=FakeFiller(), settings=SETTINGS)

    assert summary.sent == 3


async def test_AI_호출은_이미지와_함께_한_번이고_bootcamp_fill_로_남는다(
    conn: sqlite3.Connection,
) -> None:
    from tests.test_detail_images import png

    course = sesac.parse_detail(DETAILS["1197"], "1197")

    class ImageFetcher:
        async def request(self, url: str, **_: Any) -> FetchResult:
            return FetchResult(url=url, status_code=200, text="", content=png(40, 40))

    answer = {
        "short_description": " 한 줄 ",
        "content": "■ 과정 소개\n- 풀스택",
        "eligibility_and_selection_process": "  ",
        "capacity": 25,
        "manager_email": "문의없음",
    }
    client = FakeGeminiClient(json.dumps(answer))
    filler = LlmBootcampFiller(
        conn,
        ImageFetcher(),  # type: ignore[arg-type]
        settings=Settings(gemini_api_key="테스트키", gemini_model="gemini-3.5-flash"),
        client=client,
    )

    filled = await filler.fill(course)

    assert filled.short_description == "한 줄"
    assert filled.eligibility_and_selection_process is None
    assert filled.manager_email is None
    assert filled.capacity == 25
    assert len(client.calls) == 1
    rows = conn.execute("SELECT feature, ok FROM llm_calls").fetchall()
    assert [tuple(row) for row in rows] == [("bootcamp_fill", 1)]


def test_주기_수집을_켜면_잡이_걸리고_끄면_떨어진다(conn: sqlite3.Connection) -> None:
    scheduler = get_scheduler().scheduler
    bootcamp_settings.write_config(
        conn, bootcamp_settings.BootcampConfig(schedule_enabled=True, interval_hours=6)
    )
    try:
        assert schedule.sync(scheduler, conn) is True
        assert scheduler.get_job(schedule.JOB_ID) is not None

        bootcamp_settings.write_config(conn, bootcamp_settings.BootcampConfig())
        assert schedule.sync(scheduler, conn) is False
        assert scheduler.get_job(schedule.JOB_ID) is None
    finally:
        if scheduler.get_job(schedule.JOB_ID) is not None:
            scheduler.remove_job(schedule.JOB_ID)


def test_수집_주기는_1시간부터_일주일까지다(conn: sqlite3.Connection) -> None:
    with pytest.raises(bootcamp_settings.BootcampSettingError):
        bootcamp_settings.write_config(conn, bootcamp_settings.BootcampConfig(interval_hours=0))


async def test_화면에_모은_과정과_보낼_값이_보인다(
    tmp_path: pathlib.Path, conn: sqlite3.Connection
) -> None:
    await run_sesac(conn, fetcher=SesacFetcher(), filler=FakeFiller(), settings=SETTINGS)

    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(tmp_path / "jobs.db")
        try:
            yield connection
        finally:
            connection.close()

    app.dependency_overrides[settings_api.get_connection] = request_connection
    try:
        client = TestClient(app)
        page = client.get("/bootcamps")
        panel = client.get("/ui/bootcamps")
    finally:
        app.dependency_overrides.clear()

    assert page.status_code == 200
    assert 'aria-current="page"' in page.text and "부트캠프" in page.text
    assert panel.status_code == 200
    assert "모은 과정 3건" in panel.text
    assert "(성동4기)" in panel.text
    assert "성동 한 줄 소개" in panel.text


def test_store_해시는_같은_값이면_같다() -> None:
    course = sesac.parse_detail(DETAILS["1197"], "1197")

    assert store.page_hash(course, "a") == store.page_hash(course, "a")
    assert store.page_hash(course, "a") != store.page_hash(course, "b")


def test_목록은_모집_상태와_상관없이_읽고_카드의_상태를_가져온다() -> None:
    items = sesac.parse_list(ALL_LIST_HTML)

    assert len(items) == 12
    assert [item.status for item in items[:4]] == ["모집중", "모집중", "모집중", "운영중"]
    assert "searchRcrtStts" not in sesac.list_url(2)


def test_운영중_과정의_상세도_상태를_읽는다() -> None:
    course = sesac.parse_detail(DETAILS["1202"], "1202")

    assert (course.status, course.campus, course.category) == ("운영중", "성동", "디지털마케팅")


@pytest.mark.parametrize(
    ("label", "status"),
    [
        ("모집중", "RECRUITING"),
        ("모집예정", "RECRUITING"),
        ("운영중", "CLOSED"),
        ("과정종료", "CLOSED"),
    ],
)
def test_새싹_상태를_오공고_모집_상태로_옮긴다(label: str, status: str) -> None:
    assert sesac.spring_status(label) == status


async def test_한_번에_정리하는_수에_상한을_두고_모집중부터_채운다(
    conn: sqlite3.Connection,
) -> None:
    fetcher = SesacFetcher(list_html=ALL_LIST_HTML)
    filler = FakeFiller()

    first = await run_sesac(conn, fetcher=fetcher, filler=filler, settings=SETTINGS, max_fills=4)

    assert (first.listed, first.filled) == (12, 4)
    assert [title[:6] for title in filler.titles][:3] == ["(성동4기)", "Google", "중소기업부터"]
    assert any("8건은 다음 수집에서" in note for note in first.notes)
    row = conn.execute("SELECT * FROM bootcamps WHERE external_id = '1202'").fetchone()
    assert row["status_label"] == "운영중"
    assert deliver.payload(row)["status"] == "CLOSED"
    recruiting = conn.execute("SELECT * FROM bootcamps WHERE external_id = '1197'").fetchone()
    assert deliver.payload(recruiting)["status"] == "RECRUITING"


async def test_모집_상태가_바뀌면_상세_없이_같은_id_로_다시_보낸다(
    conn: sqlite3.Connection,
) -> None:
    await run_sesac(conn, fetcher=SesacFetcher(), filler=FakeFiller(), settings=SETTINGS)
    ids = iter(range(101, 200))
    seen = _spring(
        lambda request: httpx.Response(
            201 if request.method == "POST" else 200,
            json={"data": {"bootcampId": next(ids)} if request.method == "POST" else None},
        )
    )
    await deliver.deliver_pending(conn, settings=SETTINGS)
    seen.clear()
    started = LIST_HTML.replace('<li class="clr01">모집중</li>', '<li class="clr04">운영중</li>', 1)
    fetcher = SesacFetcher(list_html=started)

    summary = await run_sesac(conn, fetcher=fetcher, filler=FakeFiller(), settings=SETTINGS)
    resent = await deliver.deliver_pending(conn, settings=SETTINGS)

    assert (summary.skipped, summary.status_changed, summary.filled) == (3, 1, 0)
    assert all(sesac.LIST_PATH in url for url in fetcher.urls)
    assert resent.sent == 1
    assert [(request.method, request.url.path) for request in seen] == [
        ("PUT", "/api/v1/internal/bootcamps/101")
    ]
    assert json.loads(seen[0].content)["status"] == "CLOSED"
    assert deliver.pending(conn) == []


def test_지금_수집은_백그라운드로_시작한다(
    tmp_path: pathlib.Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import ui_bootcamps

    started: list[Settings] = []
    monkeypatch.setattr(ui_bootcamps, "_start", started.append)

    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(tmp_path / "jobs.db")
        try:
            yield connection
        finally:
            connection.close()

    app.dependency_overrides[settings_api.get_connection] = request_connection
    try:
        client = TestClient(app)
        first = client.post("/ui/bootcamps/run")
        conn.execute("INSERT INTO bootcamp_runs (trigger, status) VALUES ('manual', 'running')")
        second = client.post("/ui/bootcamps/run")
    finally:
        app.dependency_overrides.clear()

    assert "수집을 시작했다" in first.text
    assert "이미 수집하는 중이다" in second.text
    assert 'hx-trigger="every 5s"' in second.text
    assert len(started) == 1
