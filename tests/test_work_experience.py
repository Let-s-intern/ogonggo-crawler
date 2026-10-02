"""미래내일 일경험 수집·정리·전송 (2026-10-02 결정, LC-3432).

포털과 오공고를 실제로 부르지 않는다. 포털은 저장해 둔 페이지(`tests/fixtures/work-experience-*`)를
돌려주는 가짜 fetcher 가, 오공고는 `httpx.MockTransport` 가 흉내 낸다.

| 확인 | 깨지면 |
|---|---|
| 목록 카드와 상세 표의 칸·날짜를 읽는다 | 오공고 필수 칸이 비어 거절된다 |
| 목록·상세를 POST 폼으로 묻는다 | 포털이 `GET Parameter 조회를 지원하지 않습니다` 를 준다 |
| 정리가 끝났고 카드가 그대로면 상세도 AI 도 다시 부르지 않는다 | 수집마다 비용이 든다 |
| 카드의 모집기간이 바뀌면 상세를 다시 받아 다시 보낸다 | 늘어난 모집기간이 오공고에 안 간다 |
| 목록이 비었는데 총 건수가 0 이 아니면 실패다 | 포털 틀이 바뀐 날이 성공으로 보인다 |
| 고용 형태 미래내일 일경험·참여기업·지역·모집기간을 실어 등록한다 | 일반 공고로 섞인다 |
| 409 면 원문 주소로 id 를 찾아 교체한다 | id 를 잃은 프로그램을 영영 못 보낸다 |
| 마감일이 지난 프로그램은 보내지 않는다 | 이미 끝난 프로그램이 오공고에 새로 뜬다 |
| AI 가 다른 직군의 직무를 고르면 그 직군의 기타 직무다 | 오공고가 400 으로 거절한다 |
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
from collections.abc import Callable, Iterator
from datetime import date
from typing import Any

import httpx
import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from app import companies, db
from app.api import settings as settings_api
from app.config import Settings
from app.crawler.fetcher import FetchResult
from app.deliver.settings import DeliverConfig, write_config
from app.main import app
from app.scheduler import get_scheduler
from app.work_experience import deliver, portal, schedule, store
from app.work_experience import settings as work_settings
from app.work_experience.fill import (
    LlmProgramFiller,
    ProgramFill,
    ProgramFillError,
    program_text,
    region_name,
    response_model,
    settle,
)
from app.work_experience.runner import run_all, run_once
from tests.test_selector_generator import FakeClient as FakeGeminiClient

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
LIST_HTML = (FIXTURES / "work-experience-list-20261002.html").read_text(encoding="utf-8")
INTERN = "PG0031168202609230009"
PROJECT = "PG0031102202609160001"
ESG = "PG0031109202608120003"
DETAILS = {
    program_id: (FIXTURES / f"work-experience-detail-{program_id}-20261002.html").read_text(
        encoding="utf-8"
    )
    for program_id in (INTERN, PROJECT, ESG)
}
KEY = "test-internal-key"
SETTINGS = Settings(gemini_api_key="테스트키", ogonggo_internal_api_key=KEY)
# 픽스처 목록의 모집 마감은 2026-10-02 부터다. 이날을 오늘로 둔다
TODAY = "2026-10-01"


def _only(html: str, keep: int, total: int | None = None) -> str:
    """목록에서 앞의 카드 `keep` 개만 남긴다. 상세 픽스처는 세 개뿐이다."""
    soup = BeautifulSoup(html, "html.parser")
    for card in soup.select("ul.card-list > li")[keep:]:
        card.decompose()
    counter = soup.select_one("div.total strong")
    if total is not None and counter is not None:
        counter.string = str(total)
    return str(soup)


LIST_THREE = _only(LIST_HTML, 3)


class PortalFetcher:
    """목록 첫 쪽과 상세들을 돌려준다. 모르는 프로그램 번호에는 인턴형 상세를 준다.

    둘째 쪽부터는 빈 목록(`총 0 건`)이다 — 포털이 실제로 그렇게 답한다.
    """

    def __init__(self, list_html: str = LIST_THREE) -> None:
        self.list_html = list_html
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def fetch(self, url: str) -> FetchResult:
        return await self.request(url)

    async def request(
        self, url: str, *, method: str = "GET", form_body: Any = None, **_: Any
    ) -> FetchResult:
        form = dict(form_body or {})
        self.calls.append((method, url, form))
        if url == portal.LIST_URL:
            text = self.list_html if form.get("currentPageNo") == "1" else _EMPTY_LIST
            return FetchResult(url=url, status_code=200, text=text)
        program_id = form.get(portal.ID_PARAM, "")
        return FetchResult(url=url, status_code=200, text=DETAILS.get(program_id, DETAILS[INTERN]))

    @property
    def details(self) -> list[str]:
        return [form[portal.ID_PARAM] for _, url, form in self.calls if url == portal.DETAIL_URL]


_EMPTY_LIST = _only(LIST_HTML, 0, total=0)


class FakeFiller:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.titles: list[str] = []

    async def fill(self, program: portal.Program) -> ProgramFill:
        self.titles.append(program.title)
        if self.error is not None:
            raise self.error
        return ProgramFill(
            job_field="경영·사무",
            job_role="사무행정",
            industry="교육업",
            experience_type="IRRELEVANT",
            education_level="ANY",
            responsibilities="■ 하는 일\n- 사무행정",
            qualifications="- 만 15세 이상 34세 이하 미취업 청년",
            recruitment_notice="- 운영계획서 참고",
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
def fixed_today(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(deliver, "today", lambda _settings: TODAY)
    yield
    deliver.transport = None


def _spring(handler: Callable[[httpx.Request], httpx.Response]) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    deliver.transport = httpx.MockTransport(wrapped)
    return seen


def _created(ids: Iterator[int]) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(
        201 if request.method == "POST" else 200,
        json={"data": {"jobId": next(ids)} if request.method == "POST" else None},
    )


def test_목록_카드에서_번호_유형_참여기업_모집기간을_읽는다() -> None:
    items = portal.parse_list(LIST_HTML)

    assert len(items) == 12
    assert portal.total_count(LIST_HTML) == 384
    first = items[0]
    assert (first.program_id, first.type_code, first.type_label) == (INTERN, "I", "인턴형")
    assert first.title == "(엘캠퍼스) 미래내일 일경험(제로인턴) 12기 - 경영사무(사무행정)"
    assert (first.job, first.region, first.headcount) == ("경영·사무", "서울", 1)
    assert (first.recruitment_start, first.recruitment_end) == (
        date(2026, 9, 28),
        date(2026, 10, 2),
    )
    assert (first.operator, first.company) == ("(주)데이원컴퍼니", "주식회사 엘캠퍼스")
    assert first.source_url == (
        "https://yw.work24.go.kr/d/a/selectItrnPrjtEsgPrgmDtal.do?untyPrgmCtn=" + INTERN
    )


def test_상세_표의_칸과_기간을_읽는다() -> None:
    program = portal.parse_detail(DETAILS[PROJECT], PROJECT)

    assert program.title.startswith("[신한금융그룹 제주은행] DJ Bank")
    assert (program.type_label, program.region, program.job_and_headcount) == (
        "프로젝트형",
        "지역무관",
        "금융·회계(12)",
    )
    assert (program.recruitment_start, program.recruitment_end) == (
        date(2026, 9, 18),
        date(2026, 10, 5),
    )
    assert (program.work_start, program.work_end) == (date(2026, 10, 19), date(2026, 12, 13))
    assert program.value("모집정보", "선발기준내용").startswith("■ 지원 자격\n- 만 15세 이상")
    assert program.value("사전직무교육정보", "교육장소").endswith("경영관 204호")
    assert program.value("운영기관담당자정보", "전화번호") == "02-450-4249"
    assert program.value("사전직무교육정보", "전화번호") == "02-450-4248"


def test_틀이_바뀌어_프로그램명이_없으면_실패다() -> None:
    with pytest.raises(portal.PortalParseError):
        portal.parse_detail("<html><body><table></table></body></html>", INTERN)


def test_참여기업이_여럿이면_첫_기업이_회사다() -> None:
    assert (
        portal.main_company("(사)하이서울기업협회, 국립공주대학교산학협력단")
        == "(사)하이서울기업협회"
    )
    assert portal.main_company("주식회사 엘캠퍼스") == "주식회사 엘캠퍼스"


def test_AI_에게_주는_글에는_사업정보와_첨부파일이_없다() -> None:
    text = program_text(portal.parse_detail(DETAILS[ESG], ESG))

    assert "[모집정보]" in text and "선발 기간" in text
    assert "사업명" not in text
    assert "운영계획서_제품기획" not in text


@pytest.mark.parametrize(
    ("label", "name"),
    [
        ("서울", "SEOUL"),
        ("전남광주", "JEONNAM_GWANGJU"),
        ("지역무관", "NATIONWIDE"),
        ("화성", None),
    ],
)
def test_포털_지역을_오공고_시도로_옮긴다(label: str, name: str | None) -> None:
    assert region_name(label) == name


def test_응답_모양의_목록에는_빈_글자가_없다() -> None:
    model = response_model([("경영·사무", ("사무행정",))], ("교육업",), ("SEOUL_JUNG_GU",))
    schema = json.dumps(model.model_json_schema(), ensure_ascii=False)

    assert all('""' not in part.split("]", 1)[0] for part in schema.split('"enum": [')[1:])


def test_다른_직군의_직무를_고르면_그_직군의_기타_직무다() -> None:
    tree = [("경영·사무", ("사무행정", "기타경영·사무")), ("IT·개발", ("백엔드",))]

    fill = settle(
        {"job_field": "경영·사무", "job_role": "백엔드", "sub_region": "BUSAN_JUNG_GU"},
        tree,
        "SEOUL",
    )

    assert (fill.job_field, fill.job_role) == ("경영·사무", "기타경영·사무")
    assert fill.sub_region == ""


async def test_목록과_상세를_POST_폼으로_묻고_새_프로그램만_AI_로_채운다(
    conn: sqlite3.Connection,
) -> None:
    fetcher = PortalFetcher()
    filler = FakeFiller()

    first = await run_once(conn, fetcher=fetcher, filler=filler, settings=SETTINGS)
    second = await run_once(conn, fetcher=fetcher, filler=filler, settings=SETTINGS)

    assert (first.status, first.listed, first.new, first.filled) == ("success", 3, 3, 3)
    assert (second.skipped, second.filled) == (3, 0)
    assert len(filler.titles) == 3
    assert len(fetcher.details) == 3
    method, url, form = fetcher.calls[0]
    assert (method, url) == ("POST", portal.LIST_URL)
    assert form == {"currentPageNo": "1", "recordCountPerPage": "48", "sortOption": "C"}
    assert all(method == "POST" for method, _, _ in fetcher.calls)


async def test_카드의_모집기간이_바뀌면_상세를_다시_받아_다시_보낸다(
    conn: sqlite3.Connection,
) -> None:
    await run_once(conn, fetcher=PortalFetcher(), filler=FakeFiller(), settings=SETTINGS)
    ids = iter(range(1, 100))
    seen = _spring(_created(ids))
    await deliver.deliver_pending(conn, settings=SETTINGS)

    extended = LIST_THREE.replace("26-09-28 ~ 26-10-02", "26-09-28 ~ 26-10-09", 1)
    original = DETAILS[INTERN]
    DETAILS[INTERN] = original.replace("2026-09-28 ~ 2026-10-02", "2026-09-28 ~ 2026-10-09")
    try:
        fetcher = PortalFetcher(extended)
        summary = await run_once(conn, fetcher=fetcher, filler=FakeFiller(), settings=SETTINGS)
    finally:
        DETAILS[INTERN] = original
    result = await deliver.deliver_pending(conn, settings=SETTINGS)

    assert fetcher.details == [INTERN]
    assert summary.filled == 1
    assert result.sent == 1
    assert seen[-1].method == "PUT"
    assert seen[-1].url.path == "/api/v1/internal/jobs/1"
    assert json.loads(seen[-1].content)["recruitmentEndAt"] == "2026-10-09T23:59:59"


async def test_AI_가_실패하면_사유를_남기고_다음_수집에서_다시_채운다(
    conn: sqlite3.Connection,
) -> None:
    failing = await run_once(
        conn,
        fetcher=PortalFetcher(),
        filler=FakeFiller(ProgramFillError("AI 호출이 실패했다")),
        settings=SETTINGS,
    )
    retried = await run_once(conn, fetcher=PortalFetcher(), filler=FakeFiller(), settings=SETTINGS)

    assert (failing.fill_failed, failing.filled) == (3, 0)
    assert retried.filled == 3
    assert {row[0] for row in conn.execute("SELECT fill_error FROM work_experiences")} == {""}


async def test_목록이_비었는데_총_건수가_있으면_실패다(conn: sqlite3.Connection) -> None:
    broken = _only(LIST_HTML, 0)

    summary = await run_once(
        conn, fetcher=PortalFetcher(broken), filler=FakeFiller(), settings=SETTINGS
    )

    assert summary.status == "failed"
    assert "총 384건" in summary.notes[0]
    row = conn.execute("SELECT status FROM work_experience_runs").fetchone()
    assert row["status"] == "failed"


async def test_남은_프로그램이_있으면_다음_묶음이_이어서_정리한다(conn: sqlite3.Connection) -> None:
    summaries = await run_all(
        conn, fetcher=PortalFetcher(), filler=FakeFiller(), settings=SETTINGS, max_fills=2
    )

    assert [summary.filled for summary in summaries] == [2, 1]
    assert summaries[0].deferred == 1


async def test_미래내일_일경험_채용공고로_등록하고_두_번_보내지_않는다(
    conn: sqlite3.Connection,
) -> None:
    await run_once(conn, fetcher=PortalFetcher(), filler=FakeFiller(), settings=SETTINGS)
    seen = _spring(_created(iter(range(101, 200))))

    first = await deliver.deliver_pending(conn, settings=SETTINGS)
    again = await deliver.deliver_pending(conn, settings=SETTINGS)

    assert (first.sent, first.failed, again.sent) == (3, 0, 0)
    assert [request.method for request in seen] == ["POST", "POST", "POST"]
    assert seen[0].headers["X-Internal-Api-Key"] == KEY
    body = json.loads(seen[0].content)
    assert body["employmentType"] == "WORK_EXPERIENCE"
    assert body["companyName"] == "주식회사 엘캠퍼스"
    assert body["parentCompanyName"] is None
    assert body["title"] == "(엘캠퍼스) 미래내일 일경험(제로인턴) 12기 - 경영사무(사무행정)"
    assert (body["region"], body["subRegion"]) == ("SEOUL", None)
    assert body["recruitmentHeadcount"] == 1
    assert body["recruitmentType"] == "PERIOD"
    assert body["recruitmentStartAt"] == "2026-09-28T00:00:00"
    assert body["recruitmentEndAt"] == "2026-10-02T23:59:59"
    assert (body["closesWhenFilled"], body["autoCloseEnabled"]) == (False, True)
    assert (body["experienceType"], body["experienceMinYears"]) == ("IRRELEVANT", None)
    assert body["educationLevel"] == "ANY"
    assert body["applicationMethod"] == "EXTERNAL_PAGE"
    assert body["sourceUrl"].endswith("untyPrgmCtn=" + INTERN)
    notice = body["recruitmentNotice"]
    assert notice.startswith("■ 미래내일 일경험 인턴형\n- 일경험 기간: 2026-10-12 ~ 2026-12-04")
    assert "- 사전직무교육: 2026-10-07 ~ 2026-10-08 (" in notice
    assert "- 운영기관: (주)데이원컴퍼니 (김윤경, 02-508-0375)" in notice
    assert notice.endswith("- 운영계획서 참고")
    rows = conn.execute("SELECT spring_job_id FROM work_experiences ORDER BY id").fetchall()
    assert [row[0] for row in rows] == [101, 102, 103]


async def test_담당자_이메일은_문의_이메일로_보낸다(conn: sqlite3.Connection) -> None:
    item = portal.ListItem(
        program_id=ESG,
        type_code="E",
        title="ESG",
        region="서울",
        recruitment_end=date(2026, 10, 5),
        company="소셜혁신연구소",
    )
    row_id, _ = store.upsert(conn, item, portal.parse_detail(DETAILS[ESG], ESG))
    store.save_fill(conn, row_id, ProgramFill())

    body = deliver.payload(conn, store.listing(conn)[0])

    assert body["inquiryEmail"] == "shr@socialilab.net"


async def test_409_면_원문_주소로_id_를_찾아_교체한다(conn: sqlite3.Connection) -> None:
    await run_once(conn, fetcher=PortalFetcher(), filler=FakeFiller(), settings=SETTINGS)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(409, json={"message": "이미 등록된 원문 URL입니다."})
        if request.method == "GET":
            return httpx.Response(200, json={"data": {"jobId": 7}})
        return httpx.Response(200, json={"data": None})

    seen = _spring(handler)

    result = await deliver.deliver_pending(conn, settings=SETTINGS)

    assert result.sent == 3
    assert [request.method for request in seen[:3]] == ["POST", "GET", "PUT"]
    assert seen[1].url.params["sourceUrl"].startswith(portal.DETAIL_URL)
    assert {row[0] for row in conn.execute("SELECT spring_job_id FROM work_experiences")} == {7}


async def test_마감일이_지난_프로그램은_보내지_않는다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    await run_once(conn, fetcher=PortalFetcher(), filler=FakeFiller(), settings=SETTINGS)
    monkeypatch.setattr(deliver, "today", lambda _settings: "2026-10-03")
    seen = _spring(_created(iter(range(1, 10))))

    result = await deliver.deliver_pending(conn, settings=SETTINGS)

    assert (result.sent, seen) == (0, [])


async def test_거절되면_사유를_남기고_세_번까지만_자동으로_보낸다(conn: sqlite3.Connection) -> None:
    await run_once(conn, fetcher=PortalFetcher(), filler=FakeFiller(), settings=SETTINGS)
    _spring(
        lambda _: httpx.Response(400, json={"message": "[employmentType] 값이 올바르지 않습니다."})
    )

    for _ in range(deliver.MAX_ATTEMPTS):
        await deliver.deliver_pending(conn, settings=SETTINGS)

    row = conn.execute("SELECT * FROM work_experiences ORDER BY id LIMIT 1").fetchone()
    assert (row["send_status"], row["send_attempts"]) == ("failed", deliver.MAX_ATTEMPTS)
    assert "employmentType" in row["send_error"]
    assert deliver.pending(conn, TODAY) == []
    assert len(deliver.pending(conn, TODAY, retry_failed=True)) == 3


async def test_전송을_켜_두면_수집이_끝날_때_보낸다(conn: sqlite3.Connection) -> None:
    work_settings.write_config(conn, work_settings.WorkExperienceConfig(deliver_enabled=True))
    _spring(_created(iter(range(1, 10))))

    summary = await run_once(conn, fetcher=PortalFetcher(), filler=FakeFiller(), settings=SETTINGS)

    assert summary.sent == 3


async def test_참여기업을_회사로_두고_로고를_올리면_같은_id_로_다시_보낸다(
    conn: sqlite3.Connection,
) -> None:
    await run_once(conn, fetcher=PortalFetcher(), filler=FakeFiller(), settings=SETTINGS)
    seen = _spring(_created(iter(range(1, 10))))
    await deliver.deliver_pending(conn, settings=SETTINGS)
    assert companies.read(conn, "주식회사 엘캠퍼스") is not None

    companies.set_logo_url(conn, "주식회사 엘캠퍼스", "https://cdn.test/elcampus.png")
    result = await deliver.deliver_pending(conn, settings=SETTINGS)

    assert result.sent == 2
    assert [request.method for request in seen[3:]] == ["PUT", "PUT"]
    assert json.loads(seen[3].content)["logoUrl"] == "https://cdn.test/elcampus.png"
    assert deliver.pending(conn, TODAY) == []


async def test_AI_호출은_한_번이고_work_experience_fill_로_남는다(
    conn: sqlite3.Connection,
) -> None:
    program = portal.parse_detail(DETAILS[INTERN], INTERN)
    answer = {
        "experience_type": "NEWCOMER",
        "education_level": "ANY",
        "sub_region": "SEOUL_GEUMCHEON_GU",
        "responsibilities": " - 사무행정 ",
        "qualifications": "",
    }
    client = FakeGeminiClient(json.dumps(answer))
    filler = LlmProgramFiller(
        conn,
        settings=Settings(gemini_api_key="테스트키", gemini_model="gemini-3.5-flash"),
        client=client,
    )

    filled = await filler.fill(program)

    assert (filled.experience_type, filled.sub_region) == ("NEWCOMER", "SEOUL_GEUMCHEON_GU")
    assert filled.responsibilities == "- 사무행정"
    assert len(client.calls) == 1
    rows = conn.execute("SELECT feature, ok FROM llm_calls").fetchall()
    assert [tuple(row) for row in rows] == [("work_experience_fill", 1)]


def test_매일_수집을_켜면_한국_시각_잡이_걸리고_끄면_떨어진다(conn: sqlite3.Connection) -> None:
    scheduler = get_scheduler().scheduler
    work_settings.write_config(
        conn, work_settings.WorkExperienceConfig(schedule_enabled=True, run_time="09:30")
    )
    try:
        assert schedule.sync(scheduler, conn) is True
        job = scheduler.get_job(schedule.JOB_ID)
        assert job is not None
        fields = {field.name: str(field) for field in job.trigger.fields}
        assert (fields["hour"], fields["minute"]) == ("9", "30")
        assert str(job.trigger.timezone) == "Asia/Seoul"

        work_settings.write_config(conn, work_settings.WorkExperienceConfig())
        assert schedule.sync(scheduler, conn) is False
        assert scheduler.get_job(schedule.JOB_ID) is None
    finally:
        if scheduler.get_job(schedule.JOB_ID) is not None:
            scheduler.remove_job(schedule.JOB_ID)


def test_수집_시각은_HH_MM_이다(conn: sqlite3.Connection) -> None:
    with pytest.raises(work_settings.WorkExperienceSettingError):
        work_settings.write_config(conn, work_settings.WorkExperienceConfig(run_time="25:00"))


async def test_화면에_모은_프로그램과_보낼_값이_보인다(
    tmp_path: pathlib.Path, conn: sqlite3.Connection
) -> None:
    await run_once(conn, fetcher=PortalFetcher(), filler=FakeFiller(), settings=SETTINGS)

    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(tmp_path / "jobs.db")
        try:
            yield connection
        finally:
            connection.close()

    app.dependency_overrides[settings_api.get_connection] = request_connection
    try:
        client = TestClient(app)
        page = client.get("/work-experiences")
        panel = client.get("/ui/work-experiences")
    finally:
        app.dependency_overrides.clear()

    assert page.status_code == 200
    assert 'aria-current="page"' in page.text and "미래내일 일경험" in page.text
    assert panel.status_code == 200
    assert "모은 프로그램 3건" in panel.text
    assert "(엘캠퍼스) 미래내일 일경험(제로인턴) 12기" in panel.text
    assert "주식회사 엘캠퍼스" in panel.text


def test_지금_수집은_백그라운드로_시작한다(
    tmp_path: pathlib.Path, conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import ui_work_experiences

    started: list[Settings] = []
    monkeypatch.setattr(ui_work_experiences, "_start", started.append)

    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(tmp_path / "jobs.db")
        try:
            yield connection
        finally:
            connection.close()

    app.dependency_overrides[settings_api.get_connection] = request_connection
    try:
        client = TestClient(app)
        first = client.post("/ui/work-experiences/run")
        conn.execute(
            "INSERT INTO work_experience_runs (trigger, status) VALUES ('manual', 'running')"
        )
        second = client.post("/ui/work-experiences/run")
    finally:
        app.dependency_overrides.clear()

    assert "수집을 시작했다" in first.text
    assert "이미 수집하는 중이다" in second.text
    assert 'hx-trigger="every 5s"' in second.text
    assert len(started) == 1
