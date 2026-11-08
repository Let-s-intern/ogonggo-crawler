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

from app.api import site_adds, ui_site_bulk
from tests.test_ui_site_add import (  # noqa: F401 — 픽스처를 이 모듈에서도 쓴다
    LIST_URL,
    Fake,
    client,
    conn,
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
    assert "2곳 자동 수집 시작" in reports[0][1][0]


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
