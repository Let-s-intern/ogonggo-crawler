"""엑셀로 사이트 여러 곳 추가 (2026-11-08). 줄마다 한 건 추가와 같은 길을 건다.

실사이트에 나가지 않는다. 등록과 시험 수집은 한 건 추가 테스트의 가짜를 그대로 쓴다.

| 확인 | 깨지면 |
|---|---|
| 추가 창에서 일괄 창을 열고 빈 양식을 받는다 | 머리글을 어떻게 적어야 하는지 모른다 |
| 엑셀의 줄마다 걸고, 다 되면 사이트마다 자동 수집이 시작된다 | 한 곳씩 옮겨 적어야 한다 |
| 머리글 순서·이름이 조금 달라도 읽고, 계열사 O 를 읽는다 | 양식을 그대로 안 쓰면 실패한다 |
| 엑셀에서 저장한 cp949 CSV 를 읽는다 | 한글이 깨져 회사 이름이 틀린다 |
| 잘못된 줄 · 등록된 주소 · 겹친 줄은 줄 번호와 사유를 남기고 건너뛴다 | 같은 사이트가 둘 생긴다 |
| 머리글이 없거나 형식이 다르면 아무것도 걸지 않는다 | 엉뚱한 칸으로 AI 가 헛돈다 |
| 슬랙은 다 끝난 뒤 한 통이다 | 백 곳이면 백 통이 온다 |
| 로고 주소에서 받아 저장소에 올리고 그 회사 로고가 된다 | 로고를 사이트마다 따로 올려야 한다 |
| 로고를 못 받아도 사이트는 시작하고 슬랙에 적힌다 | 로고 하나 때문에 사이트가 빠진다 |
| 저장소가 비거나 주소 꼴이 틀리면 받지 않고 창에 적는다 | 조용히 로고 없이 걸린다 |
| 슬랙에 넣음 · 성공 · 실패 · 중복과 실패 목록이 간다 | 결과를 화면에서 일일이 봐야 한다 |
| 실패 사유는 첫 문장만 한 줄로 | 슬랙 목록이 사유로 길어진다 |
| 하나도 못 걸어도 슬랙은 간다 | 다 실패한 파일은 아무 소식이 없다 |
| 로고만 올려 주소를 받는다 | 회사가 없는 엑셀 담당자는 로고를 올릴 곳이 없다 |
| 우리 저장소 주소는 다시 받지 않고 붙인다 | 같은 로고가 두 벌 쌓인다 |
"""

# 한 건 추가 테스트의 픽스처를 가져와 쓴다. 인자 이름이 픽스처 이름과 같아야 한다
# ruff: noqa: F811

from __future__ import annotations

import io
import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook, load_workbook

from app import companies
from app.api import crawlers as crawlers_api
from app.api import site_adds, ui_site_bulk
from app.crawler.fetcher import FetchResult, TransportError
from app.main import app
from app.storage import s3
from app.storage import settings as store
from tests.test_ui_site_add import (  # noqa: F401 — 픽스처를 이 모듈에서도 쓴다
    LIST_URL,
    Fake,
    client,
    conn,
    fill_storage,
    finish,
    install,
    launched,
    scheduler,
)

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def xlsx(*rows: list[Any]) -> bytes:
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    for row in rows:
        sheet.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def upload(client: TestClient, data: bytes, name: str = "sites.xlsx") -> str:
    return client.post("/ui/sites/bulk", files={"sheet": (name, data, XLSX)}).text


@pytest.fixture
def reports(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[str], bool]]:
    sent: list[tuple[str, list[str], bool]] = []

    async def fake_report(name: str, lines: list[str], *, ok: bool = True) -> None:
        sent.append((name, list(lines), ok))

    monkeypatch.setattr(ui_site_bulk.work_report, "report", fake_report)
    return sent


def test_추가_창에서_일괄_창을_열고_빈_양식을_받는다(client: TestClient) -> None:
    assert 'hx-get="/ui/sites/bulk"' in client.get("/ui/sites/new").text
    assert 'name="sheet"' in client.get("/ui/sites/bulk").text

    response = client.get("/ui/sites/bulk/template")

    assert response.headers["content-type"] == XLSX
    book = load_workbook(io.BytesIO(response.content))
    headers = [cell.value for cell in book.worksheets[0][1]]
    assert headers == list(ui_site_bulk.TEMPLATE_HEADERS)
    # 양식 그대로 올려도 예시 줄이 등록되지 않는다
    assert book.worksheets[0].max_row == 1


def test_엑셀의_줄마다_걸고_다_되면_자동_수집이_시작된다(
    client: TestClient,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    launched: list[Any],
    reports: list[tuple[str, list[str], bool]],
) -> None:
    fake = Fake(conn, found=True)
    install(monkeypatch, fake)
    body = upload(
        client,
        xlsx(
            ["회사 이름", "목록 페이지 주소", "예시 공고 주소", "계열사"],
            ["가나다", "https://a.example.com/jobs", None, None],
            ["라마바", "https://b.example.com/jobs", "https://b.example.com/jobs/1", "O"],
        ),
    )

    assert "2곳" in body
    assert [add.company for add in site_adds.listed()] == ["가나다", "라마바"]
    finish(launched)

    names = [r["name"] for r in conn.execute("SELECT name FROM workflows ORDER BY id")]
    assert names == ["가나다", "라마바"]
    assert [p.detail_url for p in fake.registered] == ["", "https://b.example.com/jobs/1"]
    affiliates = conn.execute("SELECT has_affiliates FROM crawlers ORDER BY id").fetchall()
    assert [r["has_affiliates"] for r in affiliates] == [0, 1]
    # 사이트마다가 아니라 다 끝난 뒤 한 통
    assert [(name, ok) for name, _, ok in reports] == [("사이트 일괄 추가", True)]
    assert "성공 2곳" in "\n".join(reports[0][1])


def test_머리글_순서와_이름이_조금_달라도_읽는다() -> None:
    rows = ui_site_bulk.parse_rows(
        "s.xlsx",
        xlsx(
            [],
            ["목록 주소", "비고", "회사명", "계열사 여부"],
            ["https://a.example.com/jobs", "메모", "가나다", "y"],
            [None, None, None, None],
            ["https://b.example.com/jobs", "", "라마바", ""],
        ),
    )

    assert [(r.line, r.company, r.has_affiliates) for r in rows] == [
        (3, "가나다", True),
        (5, "라마바", False),
    ]


def test_엑셀에서_저장한_cp949_CSV_를_읽는다() -> None:
    data = "회사 이름,목록 페이지 주소\r\n가나다,https://a.example.com/jobs\r\n".encode("cp949")

    rows = ui_site_bulk.parse_rows("sites.csv", data)

    assert [(r.company, r.list_url) for r in rows] == [("가나다", "https://a.example.com/jobs")]


def test_잘못된_줄과_겹친_주소는_줄_번호와_사유를_남기고_건너뛴다(
    client: TestClient,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    launched: list[Any],
) -> None:
    install(monkeypatch, Fake(conn, found=True))
    conn.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES ('있음', ?, 'promoted')",
        (LIST_URL,),
    )
    conn.commit()

    body = upload(
        client,
        xlsx(
            ["회사 이름", "목록 페이지 주소"],
            ["이미", LIST_URL + "/"],
            ["주소없음", "careers.example.com"],
            ["", "https://c.example.com/jobs"],
            ["새곳", "https://d.example.com/jobs"],
            ["또새곳", "https://d.example.com/jobs"],
        ),
    )

    assert [add.company for add in site_adds.listed()] == ["새곳"]
    assert "건너뛴 줄 4개" in body
    for line in ("2번째 줄", "3번째 줄", "4번째 줄", "6번째 줄"):
        assert line in body
    assert "이미 등록됐거나 추가 중인 목록 주소" in body
    assert "앞 줄과 목록 주소가 같아요" in body


@pytest.mark.parametrize(
    ("name", "data", "message"),
    [
        ("s.xlsx", xlsx(["이름", "주소"], ["a", "https://a.example.com"]), "칸을 찾지 못했어요"),
        ("s.xlsx", xlsx(["회사 이름", "목록 페이지 주소"]), "넣을 사이트가 없어요"),
        ("s.xlsx", b"not a zip", "엑셀 파일을 열지 못했어요"),
        ("s.txt", b"x", ".xlsx 나 .csv"),
    ],
)
def test_형식이_다르면_아무것도_걸지_않는다(
    client: TestClient, launched: list[Any], name: str, data: bytes, message: str
) -> None:
    body = upload(client, data, name)

    assert message in body
    assert launched == []
    assert site_adds.listed() == []


class LogoFetcher:
    """로고 주소에 정해 둔 바이트를 돌려준다. `fail` 이면 접속 실패를 낸다."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.asked: list[str] = []

    async def request(self, url: str, **_: Any) -> FetchResult:
        self.asked.append(url)
        if self.fail:
            raise TransportError("연결이 끊겼다")
        return FetchResult(url=url, status_code=200, text="", content=b"BM-logo")


@pytest.fixture
def uploaded(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    sent: list[bytes] = []

    def upload(config: store.StorageConfig, *, data: bytes, name: str) -> str:
        sent.append(data)
        return f"http://localhost:9000/logos/{name}.png"

    monkeypatch.setattr(s3, "upload_image", upload)
    return sent


LOGO_SHEET = xlsx(
    ["회사 이름", "목록 페이지 주소", "로고 주소"],
    ["가나다", "https://a.example.com/jobs", "https://a.example.com/logo.png"],
    ["라마바", "https://b.example.com/jobs", None],
)


def test_로고_주소에서_받아_올리고_그_회사_로고가_된다(
    client: TestClient,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    launched: list[Any],
    reports: list[tuple[str, list[str], bool]],
    uploaded: list[bytes],
) -> None:
    fill_storage(conn)
    install(monkeypatch, Fake(conn, found=True))
    fetcher = LogoFetcher()
    app.dependency_overrides[crawlers_api.get_crawl_fetcher] = lambda: fetcher

    assert "로고 1곳 함께" in upload(client, LOGO_SHEET)
    finish(launched)

    assert fetcher.asked == ["https://a.example.com/logo.png"]
    assert uploaded == [b"BM-logo"]
    logo = companies.read(conn, "가나다")
    assert logo is not None and (logo.logo_url or "").startswith("http://localhost:9000/")
    other = companies.read(conn, "라마바")
    assert other is None or not other.logo_url
    assert not any("로고" in line for line in reports[0][1])


def test_로고를_못_받아도_사이트는_시작하고_슬랙에_적힌다(
    client: TestClient,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    launched: list[Any],
    reports: list[tuple[str, list[str], bool]],
    uploaded: list[bytes],
) -> None:
    fill_storage(conn)
    install(monkeypatch, Fake(conn, found=True))
    app.dependency_overrides[crawlers_api.get_crawl_fetcher] = lambda: LogoFetcher(fail=True)

    upload(client, LOGO_SHEET)
    finish(launched)

    names = [r["name"] for r in conn.execute("SELECT name FROM workflows ORDER BY id")]
    assert names == ["가나다", "라마바"]
    assert uploaded == []
    text = "\n".join(reports[0][1])
    assert "로고 못 올림 (1곳" in text and "\n가나다 — 로고를 받지 못했어요" in text


def test_저장소가_비거나_주소_꼴이_틀리면_로고를_받지_않고_창에_적는다(
    client: TestClient,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    launched: list[Any],
    reports: list[tuple[str, list[str], bool]],
) -> None:
    install(monkeypatch, Fake(conn, found=True))
    fetcher = LogoFetcher()
    app.dependency_overrides[crawlers_api.get_crawl_fetcher] = lambda: fetcher

    body = upload(client, LOGO_SHEET)
    assert "로고 없이 건 줄 1개" in body and "파일 저장소가 비어" in body

    fill_storage(conn)
    sheet = xlsx(
        ["회사 이름", "목록 페이지 주소", "로고"],
        ["사아자", "https://c.example.com/jobs", "logo.png"],
    )
    body = upload(client, sheet)
    assert "로고 없이 건 줄 1개" in body and "http:// 나 https://" in body

    finish(launched)
    assert fetcher.asked == []
    assert len(site_adds.listed()) == 0


class FailFor(Fake):
    """`failing` 목록 주소만 시험 수집이 안 된다."""

    def __init__(self, conn: sqlite3.Connection, failing: str) -> None:
        super().__init__(conn, found=True)
        self.failing = failing

    async def run(self, crawler_id: int, conn: sqlite3.Connection, *_: Any) -> Any:
        row = conn.execute("SELECT list_url FROM crawlers WHERE id = ?", (crawler_id,)).fetchone()
        self.found = row["list_url"] != self.failing
        return await super().run(crawler_id, conn)


def test_슬랙에_넣음_성공_실패_중복과_실패_목록이_간다(
    client: TestClient,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    launched: list[Any],
    reports: list[tuple[str, list[str], bool]],
) -> None:
    install(monkeypatch, FailFor(conn, "https://b.example.com/jobs"))
    conn.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES ('있음', ?, 'promoted')",
        (LIST_URL,),
    )
    conn.commit()

    upload(
        client,
        xlsx(
            ["회사 이름", "목록 페이지 주소"],
            ["가나다", "https://a.example.com/jobs"],
            ["라마바", "https://b.example.com/jobs"],
            ["주소틀림", "b.example.com"],
            ["이미", LIST_URL],
            ["또가나다", "https://a.example.com/jobs/"],
        ),
    )
    finish(launched)

    name, lines, ok = reports[0]
    assert (name, ok) == ("사이트 일괄 추가", False)
    assert lines[:5] == [
        "파일: sites.xlsx",
        "넣음 5곳",
        "성공 1곳 (시험 수집이 되어 자동 수집 시작)",
        "실패 2곳 (입력 오류 1, 등록·시험 수집 실패 1)",
        "중복 2곳",
    ]
    assert lines[5] == "실패 목록 (2곳)"
    assert lines[6].startswith("3번째 줄 라마바 — ")
    assert lines[7].startswith("4번째 줄 주소틀림 — 목록 페이지 주소")
    assert len(lines) == 8


def test_실패_사유는_첫_문장만_한_줄로() -> None:
    assert (
        ui_site_bulk.one_line(
            "이 주소에서 공고 목록을 찾지 못했어요. 채용공고 목록이 보이는 페이지 주소인지"
            " 확인해 주세요"
        )
        == "이 주소에서 공고 목록을 찾지 못했어요"
    )
    long = ui_site_bulk.one_line("로고를 받지 못했어요: " + "x" * 200)
    assert len(long) == ui_site_bulk.REASON_CHARS and long.endswith("…")
    assert "\n" not in ui_site_bulk.one_line("첫 줄\n둘째 줄")


def test_하나도_못_걸어도_슬랙은_간다(
    client: TestClient,
    launched: list[Any],
    reports: list[tuple[str, list[str], bool]],
) -> None:
    upload(client, xlsx(["회사 이름", "목록 페이지 주소"], ["주소틀림", "nope"]))
    finish(launched)

    assert reports[0][1][1:5] == [
        "넣음 1곳",
        "성공 0곳 (시험 수집이 되어 자동 수집 시작)",
        "실패 1곳 (입력 오류 1, 등록·시험 수집 실패 0)",
        "중복 0곳",
    ]


def test_로고만_올려_주소를_받는다(
    client: TestClient,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert "파일 저장소" in client.get("/ui/sites/bulk/logos").text
    fill_storage(conn)

    def upload_image(config: store.StorageConfig, *, data: bytes, name: str) -> str:
        if data == b"broken":
            raise s3.StorageError("not_an_image", "이미지가 아니다")
        return f"http://localhost:9000/logos/{name}.png"

    monkeypatch.setattr(s3, "upload_image", upload_image)
    assert 'hx-get="/ui/sites/bulk/logos"' in client.get("/ui/sites/bulk").text
    assert 'name="logos"' in client.get("/ui/sites/bulk/logos").text
    body = client.post(
        "/ui/sites/bulk/logos",
        files=[
            ("logos", ("가나다.png", b"PNG-logo", "image/png")),
            ("logos", ("깨짐.png", b"broken", "image/png")),
        ],
    ).text

    assert "2장 중 1장을 올렸어요" in body
    assert "http://localhost:9000/logos/company_name/" in body
    assert "이미지가 아니다" in body
    # 회사에 붙이지 않는다. 회사는 사이트를 넣을 때 생긴다
    assert companies.read(conn, "가나다") is None


def test_우리_저장소_주소는_다시_받지_않고_붙인다(
    client: TestClient,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    launched: list[Any],
    uploaded: list[bytes],
) -> None:
    fill_storage(conn)
    install(monkeypatch, Fake(conn, found=True))
    fetcher = LogoFetcher()
    app.dependency_overrides[crawlers_api.get_crawl_fetcher] = lambda: fetcher
    ours = "http://localhost:9000/logos/company_name/abc.png"

    upload(
        client,
        xlsx(
            ["회사 이름", "목록 페이지 주소", "로고 주소"],
            ["가나다", "https://a.example.com/jobs", ours],
        ),
    )
    finish(launched)

    assert fetcher.asked == [] and uploaded == []
    company = companies.read(conn, "가나다")
    assert company is not None and company.logo_url == ours
