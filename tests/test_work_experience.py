"""미래내일 일경험 CSV 올리기·정리·전송 (2026-11-16 결정, LC-3605).

오공고와 AI 를 실제로 부르지 않는다. CSV 는 공공데이터 파일을 포털 목록과 매핑한 것의 세 줄이다
(`tests/fixtures/work-experience-20261116.csv`, 인턴형·프로젝트형·ESG지원형). 오공고는
`httpx.MockTransport` 가 흉내 낸다.

| 확인 | 깨지면 |
|---|---|
| CSV 칸을 오공고 칸과 AI 에게 줄 표로 옮긴다 | 오공고 필수 칸이 비어 거절된다 |
| 공공데이터 원본 인코딩 CP949 도 읽는다 | 받은 파일을 손으로 바꿔야 한다 |
| 프로그램번호가 없는 CSV 는 올리지 않는다 | 매핑하지 않은 원본이 주소 없이 들어간다 |
| 같은 CSV 를 다시 올리면 AI 를 다시 부르지 않는다 | 올릴 때마다 비용이 든다 |
| 모집이 끝난 줄은 넣지 않는다 | 보내지도 않을 프로그램에 AI 비용이 든다 |
| 모집기간이 늘어난 줄은 다시 정리해 같은 id 로 교체한다 | 늘어난 기간이 오공고에 안 간다 |
| 예전 포털 수집이 보낸 프로그램은 같은 행으로 이어진다 | 오공고에 같은 공고가 두 번 생긴다 |
| 공유 주소를 원문 주소로 보낸다 | 지원하기 버튼이 포털 오류 화면으로 간다 |
| 409 면 원문 주소로 id 를 찾아 교체한다 | id 를 잃은 프로그램을 영영 못 보낸다 |
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
from fastapi.testclient import TestClient

from app import companies, db
from app.api import settings as settings_api
from app.config import Settings
from app.deliver.settings import DeliverConfig, write_config
from app.main import app
from app.work_experience import deliver, source, store
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
CSV_BYTES = (FIXTURES / "work-experience-20261116.csv").read_bytes()
CSV_TEXT = CSV_BYTES.decode("utf-8-sig")
INTERN = "PG0020426202611040004"
PROJECT = "PG0020418202610300002"
ESG = "PG0031200202609160002"
INTERN_SHARE = "https://yw.work24.go.kr/d/a/prgmDtl/mi6KvX9fyW"
KEY = "test-internal-key"
SETTINGS = Settings(gemini_api_key="테스트키", ogonggo_internal_api_key=KEY)
# 픽스처의 모집 마감은 2026-11-18 부터다
TODAY = "2026-11-16"


def _programs(text: str = CSV_TEXT) -> list[tuple[source.ListItem, source.Program]]:
    return source.parse_csv(text.encode("utf-8")).programs


def _replace(text: str, old: str, new: str) -> str:
    assert old in text
    return text.replace(old, new, 1)


class FakeFiller:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.titles: list[str] = []

    async def fill(self, program: source.Program) -> ProgramFill:
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


async def _upload(
    conn: sqlite3.Connection, text: str = CSV_TEXT, filler: FakeFiller | None = None, **kwargs: Any
) -> Any:
    return await run_once(
        conn,
        programs=_programs(text),
        filler=filler or FakeFiller(),
        settings=SETTINGS,
        **kwargs,
    )


def test_CSV_칸을_고정_칸과_AI_에게_줄_표로_옮긴다() -> None:
    parsed = source.parse_csv(CSV_BYTES)

    assert (parsed.rows, parsed.skipped) == (3, [])
    item, program = parsed.programs[0]
    assert (item.program_id, item.type_code, item.type_label) == (INTERN, "I", "인턴형")
    assert item.title == "[충주의료원] 경영·사무 일경험 프로그램_10기"
    assert (item.region, item.headcount) == ("충북", 1)
    assert (item.recruitment_start, item.recruitment_end) == (date(2026, 11, 5), date(2026, 11, 18))
    assert (item.operator, item.company) == ("충주상공회의소", "충청북도충주의료원")
    assert item.share_url == INTERN_SHARE
    assert item.source_url == (
        "https://yw.work24.go.kr/d/a/selectItrnPrjtEsgPrgmDtal.do?untyPrgmCtn=" + INTERN
    )
    assert program.value("운영기관담당자정보", "전화번호") == "043-843-7003"
    assert program.value("사전직무교육정보", "교육기관") == "서울현대교육재단"
    assert program.value("모집정보", "모집기간") == "2026-11-05 ~ 2026-11-18"
    assert program.value("모집정보", "모집내용")
    assert [entry.type_code for entry, _ in parsed.programs] == ["I", "P", "E"]


def test_공공데이터_원본_인코딩_CP949_도_읽는다() -> None:
    parsed = source.parse_csv(CSV_TEXT.encode("cp949", errors="replace"))

    assert [item.program_id for item, _ in parsed.programs] == [INTERN, PROJECT, ESG]


def test_프로그램번호_칸이_없으면_매핑하지_않은_CSV_다() -> None:
    original = "지역,프로그램명,모집종료일\n서울,인턴,2026-11-30\n"

    with pytest.raises(source.SourceError, match="프로그램번호"):
        source.parse_csv(original.encode("cp949"))


def test_프로그램번호가_없거나_겹치는_줄은_사유와_함께_건너뛴다() -> None:
    header, first, *rest = CSV_TEXT.splitlines(keepends=True)
    no_id = first.replace(INTERN, "", 1)

    parsed = source.parse_csv((header + first + no_id + first + "".join(rest)).encode())

    assert [item.program_id for item, _ in parsed.programs] == [INTERN, PROJECT, ESG]
    assert parsed.skipped[0].startswith("3번째 줄: 프로그램번호가 없다")
    assert parsed.skipped[1].startswith("4번째 줄: 앞 줄과 프로그램번호가 같다")


def test_공유_주소는_포털_주소만_받고_443_을_덜어_낸다() -> None:
    text = _replace(CSV_TEXT, INTERN_SHARE, "https://yw.work24.go.kr:443/d/a/prgmDtl/mi6KvX9fyW")
    text = _replace(text, "https://yw.work24.go.kr/d/a/prgmDtl/2eoxrKo0uO", "https://evil.test/x")

    items = [item for item, _ in _programs(text)]

    assert [item.share_url for item in items[:2]] == [INTERN_SHARE, ""]


def test_AI_에게_주는_글에는_빈_칸이_없다() -> None:
    _, program = _programs()[2]

    text = program_text(program)

    assert "[모집정보]" in text and "[운영기관담당자정보]" in text
    assert "교육기관:" not in text
    assert all(line.split(": ", 1)[-1].strip() for line in text.splitlines() if ": " in line)


@pytest.mark.parametrize(
    ("label", "name"),
    [
        ("서울", "SEOUL"),
        ("충북", "CHUNGBUK"),
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


async def test_CSV_를_넣어_AI_로_채우고_다시_올리면_건너뛴다(conn: sqlite3.Connection) -> None:
    filler = FakeFiller()

    first = await _upload(conn, filler=filler)
    second = await _upload(conn, filler=filler)

    assert (first.status, first.listed, first.new, first.filled) == ("success", 3, 3, 3)
    assert (second.listed, second.new, second.skipped, second.filled) == (3, 0, 3, 0)
    assert len(filler.titles) == 3
    runs = conn.execute("SELECT status, listed_count FROM work_experience_runs").fetchall()
    assert [tuple(row) for row in runs] == [("success", 3), ("success", 3)]


async def test_모집이_끝난_줄은_넣지_않는다(conn: sqlite3.Connection) -> None:
    closed = _replace(CSV_TEXT, "2026-11-18", "2026-11-15")

    summary = await _upload(conn, closed)

    assert (summary.new, summary.skipped, summary.filled) == (2, 1, 2)
    ids = {row[0] for row in conn.execute("SELECT external_id FROM work_experiences")}
    assert ids == {PROJECT, ESG}


async def test_모집기간이_늘어난_줄은_다시_정리해_같은_id_로_교체한다(
    conn: sqlite3.Connection,
) -> None:
    await _upload(conn)
    seen = _spring(_created(iter(range(1, 100))))
    await deliver.deliver_pending(conn, settings=SETTINGS)

    filler = FakeFiller()
    extended = CSV_TEXT.replace("2026-11-18", "2026-11-25")
    summary = await _upload(conn, extended, filler=filler)
    result = await deliver.deliver_pending(conn, settings=SETTINGS)

    assert (summary.new, summary.skipped, summary.filled) == (0, 2, 1)
    assert filler.titles == ["[충주의료원] 경영·사무 일경험 프로그램_10기"]
    assert result.sent == 1
    assert (seen[-1].method, seen[-1].url.path) == ("PUT", "/api/v1/internal/jobs/1")
    assert json.loads(seen[-1].content)["recruitmentEndAt"] == "2026-11-25T23:59:59"


async def test_예전_포털_수집이_보낸_프로그램은_같은_행으로_이어진다(
    conn: sqlite3.Connection,
) -> None:
    item, program = _programs()[0]
    old_item = source.ListItem(
        program_id=INTERN, type_code="I", title=item.title, recruitment_end=date(2026, 11, 18)
    )
    row_id, _ = store.upsert(conn, old_item, program)
    store.save_fill(conn, row_id, ProgramFill())
    conn.execute(
        "UPDATE work_experiences SET spring_job_id = 41, sent_hash = page_hash WHERE id = ?",
        (row_id,),
    )
    seen = _spring(_created(iter(range(1, 10))))

    await _upload(conn)
    await deliver.deliver_pending(conn, settings=SETTINGS)

    assert conn.execute("SELECT count(*) FROM work_experiences").fetchone()[0] == 3
    put = next(request for request in seen if request.method == "PUT")
    assert put.url.path == "/api/v1/internal/jobs/41"
    assert json.loads(put.content)["sourceUrl"] == INTERN_SHARE


async def test_AI_가_실패하면_사유를_남기고_이어서_정리하면_다시_채운다(
    conn: sqlite3.Connection,
) -> None:
    failing = await _upload(conn, filler=FakeFiller(ProgramFillError("AI 호출이 실패했다")))
    retried = await run_once(conn, filler=FakeFiller(), settings=SETTINGS)

    assert (failing.fill_failed, failing.filled) == (3, 0)
    assert f"{INTERN}: AI 호출이 실패했다" in failing.notes
    assert (retried.listed, retried.filled) == (0, 3)
    assert {row[0] for row in conn.execute("SELECT fill_error FROM work_experiences")} == {""}


async def test_남은_프로그램이_있으면_다음_묶음이_이어서_정리한다(conn: sqlite3.Connection) -> None:
    summaries = await run_all(
        conn, programs=_programs(), filler=FakeFiller(), settings=SETTINGS, max_fills=2
    )

    assert [summary.filled for summary in summaries] == [2, 1]
    assert [summary.listed for summary in summaries] == [3, 0]
    assert summaries[0].deferred == 1


async def test_미래내일_일경험_채용공고로_등록하고_두_번_보내지_않는다(
    conn: sqlite3.Connection,
) -> None:
    await _upload(conn)
    seen = _spring(_created(iter(range(101, 200))))

    first = await deliver.deliver_pending(conn, settings=SETTINGS)
    again = await deliver.deliver_pending(conn, settings=SETTINGS)

    assert (first.sent, first.failed, again.sent) == (3, 0, 0)
    assert [request.method for request in seen] == ["POST", "POST", "POST"]
    assert seen[0].headers["X-Internal-Api-Key"] == KEY
    body = json.loads(seen[0].content)
    assert body["employmentType"] == "WORK_EXPERIENCE"
    assert body["companyName"] == "충청북도충주의료원"
    assert body["title"] == "[충주의료원] 경영·사무 일경험 프로그램_10기"
    assert (body["region"], body["subRegion"]) == ("CHUNGBUK", None)
    assert body["recruitmentHeadcount"] == 1
    assert body["recruitmentStartAt"] == "2026-11-05T00:00:00"
    assert body["recruitmentEndAt"] == "2026-11-18T23:59:59"
    assert body["applicationMethod"] == "EXTERNAL_PAGE"
    assert body["sourceUrl"] == INTERN_SHARE
    assert body["inquiryEmail"] is None
    notice = body["recruitmentNotice"]
    assert notice.startswith("■ 미래내일 일경험 인턴형\n- 일경험 기간: ")
    assert "- 사전직무교육: 2026-11-26 ~ " in notice and "(서울현대교육재단)" in notice
    assert "- 운영기관: 충주상공회의소 (이*미, 043-843-7003)" in notice
    assert json.loads(seen[2].content)["companyName"] == "목포문화방송 주식회사"
    rows = conn.execute("SELECT spring_job_id FROM work_experiences ORDER BY id").fetchall()
    assert [row[0] for row in rows] == [101, 102, 103]


async def test_409_에_공유_주소로_못_찾으면_상세_주소로_찾는다(conn: sqlite3.Connection) -> None:
    await _upload(conn)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(409, json={"message": "이미 등록된 원문 URL입니다."})
        if request.method == "GET":
            if request.url.params["sourceUrl"].startswith(source.DETAIL_URL):
                return httpx.Response(200, json={"data": {"jobId": 7}})
            return httpx.Response(404, json={"message": "없다"})
        return httpx.Response(200, json={"data": None})

    seen = _spring(handler)

    result = await deliver.deliver_pending(conn, settings=SETTINGS)

    assert result.sent == 3
    assert [request.method for request in seen[:4]] == ["POST", "GET", "GET", "PUT"]
    assert seen[1].url.params["sourceUrl"] == INTERN_SHARE
    assert json.loads(seen[3].content)["sourceUrl"] == INTERN_SHARE
    assert {row[0] for row in conn.execute("SELECT spring_job_id FROM work_experiences")} == {7}


async def test_마감일이_지난_프로그램은_보내지_않는다(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _upload(conn)
    monkeypatch.setattr(deliver, "today", lambda _settings: "2026-12-01")
    seen = _spring(_created(iter(range(1, 10))))

    result = await deliver.deliver_pending(conn, settings=SETTINGS)

    assert (result.sent, seen) == (0, [])


async def test_거절되면_사유를_남기고_세_번까지만_자동으로_보낸다(conn: sqlite3.Connection) -> None:
    await _upload(conn)
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


async def test_전송을_켜_두면_정리가_끝날_때_보낸다(conn: sqlite3.Connection) -> None:
    work_settings.write_config(conn, work_settings.WorkExperienceConfig(deliver_enabled=True))
    _spring(_created(iter(range(1, 10))))

    summary = await _upload(conn)

    assert summary.sent == 3


async def test_참여기업을_회사로_두고_로고를_올리면_같은_id_로_다시_보낸다(
    conn: sqlite3.Connection,
) -> None:
    await _upload(conn)
    seen = _spring(_created(iter(range(1, 10))))
    await deliver.deliver_pending(conn, settings=SETTINGS)
    assert companies.read(conn, "충청북도충주의료원") is not None

    companies.set_logo_url(conn, "충청북도충주의료원", "https://cdn.test/chungju.png")
    result = await deliver.deliver_pending(conn, settings=SETTINGS)

    assert result.sent == 1
    assert [request.method for request in seen[3:]] == ["PUT"]
    assert json.loads(seen[3].content)["logoUrl"] == "https://cdn.test/chungju.png"
    assert deliver.pending(conn, TODAY) == []


async def test_AI_호출은_한_번이고_work_experience_fill_로_남는다(
    conn: sqlite3.Connection,
) -> None:
    item, program = _programs()[0]
    row_id, _ = store.upsert(conn, item, program)
    answer = {
        "experience_type": "NEWCOMER",
        "education_level": "ANY",
        "responsibilities": " - 사무행정 ",
        "qualifications": "",
    }
    client = FakeGeminiClient(json.dumps(answer))
    filler = LlmProgramFiller(
        conn,
        settings=Settings(gemini_api_key="테스트키", gemini_model="gemini-3.5-flash"),
        client=client,
    )

    row = conn.execute("SELECT * FROM work_experiences WHERE id = ?", (row_id,)).fetchone()
    filled = await filler.fill(store.program_of(row))

    assert filled.experience_type == "NEWCOMER"
    assert filled.responsibilities == "- 사무행정"
    assert len(client.calls) == 1
    rows = conn.execute("SELECT feature, ok FROM llm_calls").fetchall()
    assert [tuple(row) for row in rows] == [("work_experience_fill", 1)]


@pytest.fixture
def client(tmp_path: pathlib.Path, conn: sqlite3.Connection) -> Iterator[TestClient]:
    def request_connection() -> Iterator[sqlite3.Connection]:
        connection = db.connect(tmp_path / "jobs.db")
        try:
            yield connection
        finally:
            connection.close()

    app.dependency_overrides[settings_api.get_connection] = request_connection
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


async def test_화면에_넣은_프로그램과_보낼_값이_보인다(
    conn: sqlite3.Connection, client: TestClient
) -> None:
    await _upload(conn)

    page = client.get("/work-experiences")
    panel = client.get("/ui/work-experiences")

    assert page.status_code == 200
    assert 'aria-current="page"' in page.text and "미래내일 일경험" in page.text
    assert panel.status_code == 200
    assert "넣은 프로그램 3건" in panel.text
    assert "[충주의료원] 경영·사무 일경험 프로그램_10기" in panel.text
    assert "CSV 올리기" in panel.text and "지금 수집" not in panel.text


def test_CSV_를_올리면_백그라운드로_정리를_시작한다(
    conn: sqlite3.Connection, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import ui_work_experiences

    started: list[tuple[Any, ...]] = []
    monkeypatch.setattr(ui_work_experiences, "_start", lambda *args: started.append(args))

    uploaded = client.post(
        "/ui/work-experiences/upload", files={"sheet": ("mapped.csv", CSV_BYTES, "text/csv")}
    )
    original = client.post(
        "/ui/work-experiences/upload",
        files={"sheet": ("raw.csv", "프로그램명,모집종료일\n인턴,2026-11-30\n".encode("cp949"))},
    )
    conn.execute("INSERT INTO work_experience_runs (trigger, status) VALUES ('manual', 'running')")
    busy = client.post(
        "/ui/work-experiences/upload", files={"sheet": ("mapped.csv", CSV_BYTES, "text/csv")}
    )

    assert "CSV 3건을 올렸다" in uploaded.text
    assert "프로그램번호 칸이 없다" in original.text
    assert "이미 정리하는 중이다" in busy.text
    assert 'hx-trigger="every 5s"' in busy.text
    assert len(started) == 1
    assert [item.program_id for item, _ in started[0][1]] == [INTERN, PROJECT, ESG]


def test_전송_켜기를_저장한다(conn: sqlite3.Connection, client: TestClient) -> None:
    saved = client.put("/ui/work-experiences/settings", data={"deliver_enabled": "1"})

    assert "저장했다" in saved.text
    assert work_settings.read_config(conn).deliver_enabled is True
