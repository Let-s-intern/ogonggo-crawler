"""엑셀 한 장으로 사이트 여러 곳을 한꺼번에 추가한다 (2026-11-08).

사이트 추가 창은 한 번에 한 곳이다. 넣을 곳을 엑셀에 정리해 두고 하나씩 옮겨 적는 일이 반복돼서,
파일을 올리면 줄마다 같은 사이트 추가를 건다. 걸고 나면 한 건 추가와 똑같다 — 사이트 목록 맨 위에
진행이 보이고, 동시에 두 곳씩 돌고 나머지는 "기다리는 중" 으로 줄을 선다 (`app/api/site_adds.py`).
등록 · 시험 수집 · 자동 수집 시작은 `ui_site_add._work` 를 그대로 부른다. 따로 만들면 한 건 추가에서
되는 일이 여기서 안 되는 상태가 생긴다.

## 파일

.xlsx 와 .csv 를 받는다. 첫 시트의 첫 줄이 머리글이고, 칸 순서는 보지 않고 머리글 이름으로
찾는다 (`HEADERS`). 회사 이름과 목록 페이지 주소만 있으면 되고 예시 공고 주소 · 계열사는 비워도
된다. 엑셀에서 CSV 로 저장하면 cp949 로 나오므로 utf-8 이 아니면 cp949 로 읽는다.

로고는 받지 않는다. 파일 칸을 줄마다 둘 수 없다. 회사 화면이나 사이트 패널에서 올린다.

## 거르는 줄

줄마다 한 건 추가와 같은 입력 검사를 한다 (`ui_site_add.check_input`). 이미 등록된 목록 주소와
같은 파일 안에서 겹친 주소도 건너뛴다 — 같은 사이트를 두 번 만들면 공고가 두 번 들어온다.
건너뛴 줄은 몇 번째 줄인지와 사유를 창에 남기고, 나머지 줄은 그대로 건다.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import sqlite3
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import HTMLResponse, Response
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font

from app.api import crawlers, site_adds, workflows
from app.api.ui import render
from app.api.ui_site_add import (
    DEFAULT_INTERVAL,
    _work,
    check_input,
    get_add_connect,
    get_add_launcher,
)
from app.crawler.fetcher import FetchPolicy
from app.notify import work_report
from app.scheduler import WorkflowScheduler

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ui"], include_in_schema=False)

# 한 번에 올릴 수 있는 줄 수. 줄마다 AI 로 셀렉터를 만든다 — 실수로 큰 파일을 올려 비용이 새지 않게
MAX_ROWS = 200
MAX_BYTES = 2 * 1024 * 1024

COMPANY = "company"
LIST_URL = "list_url"
DETAIL_URL = "detail_url"
AFFILIATES = "has_affiliates"

# 머리글 이름 → 칸. 띄어쓰기는 지우고 비교한다. 양식의 이름이 맨 앞이다
HEADERS: dict[str, tuple[str, ...]] = {
    COMPANY: ("회사이름", "회사", "회사명", "그룹이름"),
    LIST_URL: ("목록페이지주소", "목록주소", "목록url", "채용목록주소"),
    DETAIL_URL: ("예시공고주소", "공고주소", "상세주소", "상세url"),
    AFFILIATES: ("계열사", "계열사여부", "그룹채용"),
}
TEMPLATE_HEADERS = ("회사 이름", "목록 페이지 주소", "예시 공고 주소", "계열사")

# 계열사 칸에서 켬으로 읽는 값
YES = {"o", "y", "yes", "예", "네", "1", "true", "v", "✓", "✔", "켬", "있음"}


class SheetError(ValueError):
    """파일을 줄로 읽지 못했다. 메시지는 창에 그대로 나간다."""


@dataclass
class Row:
    line: int
    company: str
    list_url: str
    detail_url: str
    has_affiliates: bool


@dataclass
class Skipped:
    line: int
    label: str
    reason: str


def _key(text: object) -> str:
    return "".join(str(text or "").split()).lower()


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip()


def _read_table(filename: str, data: bytes) -> list[list[str]]:
    """파일을 칸 문자열의 줄 목록으로. 빈 줄도 남긴다 — 줄 번호가 엑셀과 맞아야 한다."""
    name = filename.lower()
    if name.endswith(".csv"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = data.decode("cp949")
            except UnicodeDecodeError as exc:
                raise SheetError("CSV 글자 인코딩을 읽지 못했어요. UTF-8 로 저장해 주세요") from exc
        return [[_cell(c) for c in row] for row in csv.reader(io.StringIO(text))]
    if name.endswith(".xlsx"):
        try:
            book = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as exc:  # noqa: BLE001 — 깨진 파일의 예외 종류가 여럿이다
            raise SheetError("엑셀 파일을 열지 못했어요. .xlsx 로 다시 저장해 주세요") from exc
        try:
            sheet = book.worksheets[0]
            return [[_cell(c) for c in row] for row in sheet.iter_rows(values_only=True)]
        finally:
            book.close()
    raise SheetError(".xlsx 나 .csv 파일만 올릴 수 있어요")


def parse_rows(filename: str, data: bytes) -> list[Row]:
    """머리글로 칸을 찾아 내용이 있는 줄만 돌려준다. `line` 은 엑셀에서 보이는 줄 번호다."""
    table = _read_table(filename, data)
    start = next((i for i, row in enumerate(table) if any(row)), None)
    if start is None:
        raise SheetError("파일이 비어 있어요")
    columns: dict[str, int] = {}
    for index, header in enumerate(table[start]):
        for field_name, names in HEADERS.items():
            if _key(header) in names and field_name not in columns:
                columns[field_name] = index
    missing = [
        label
        for label, f in zip(TEMPLATE_HEADERS, (COMPANY, LIST_URL), strict=False)
        if f not in columns
    ]
    if missing:
        raise SheetError(
            f"첫 줄에서 {' · '.join(missing)} 칸을 찾지 못했어요. 양식을 받아 머리글을 맞춰 주세요"
        )

    def at(row: list[str], field_name: str) -> str:
        index = columns.get(field_name)
        return row[index] if index is not None and index < len(row) else ""

    rows = [
        Row(
            line=start + 1 + offset + 1,
            company=at(row, COMPANY),
            list_url=at(row, LIST_URL),
            detail_url=at(row, DETAIL_URL),
            has_affiliates=_key(at(row, AFFILIATES)) in YES,
        )
        for offset, row in enumerate(table[start + 1 :])
        if any(row)
    ]
    if not rows:
        raise SheetError("머리글 아래에 넣을 사이트가 없어요")
    if len(rows) > MAX_ROWS:
        raise SheetError(f"한 번에 {MAX_ROWS}곳까지 올릴 수 있어요. 지금 {len(rows)}곳이에요")
    return rows


def _same_url(url: str) -> str:
    return url.strip().rstrip("/").lower()


def registered_urls(conn: sqlite3.Connection) -> set[str]:
    """이미 있는 크롤러와 지금 추가 중인 사이트의 목록 주소."""
    urls = {_same_url(r["list_url"]) for r in conn.execute("SELECT list_url FROM crawlers")}
    urls |= {_same_url(add.list_url) for add in site_adds.listed() if add.running}
    return urls


def sort_rows(rows: Iterable[Row], taken: set[str]) -> tuple[list[Row], list[Skipped]]:
    """걸 줄과 건너뛸 줄로 나눈다. `taken` 은 이미 쓰인 목록 주소다."""
    seen = set(taken)
    ready: list[Row] = []
    skipped: list[Skipped] = []
    for row in rows:
        label = row.company or row.list_url or "(비어 있음)"
        problem = check_input(row.list_url, row.company, row.detail_url)
        if not problem and _same_url(row.list_url) in seen:
            problem = (
                "이미 등록됐거나 추가 중인 목록 주소예요"
                if _same_url(row.list_url) in taken
                else "이 파일의 앞 줄과 목록 주소가 같아요"
            )
        if problem:
            skipped.append(Skipped(row.line, label, problem))
            continue
        seen.add(_same_url(row.list_url))
        ready.append(row)
    return ready, skipped


async def _all(
    adds: list[site_adds.SiteAdd],
    work: Callable[[site_adds.SiteAdd], Coroutine[Any, Any, None]],
    skipped: int,
) -> None:
    """한꺼번에 걸고 다 끝나면 슬랙으로 한 통 보낸다. 동시에 도는 수는 `site_adds.slot` 이
    막는다.
    """
    await asyncio.gather(*(work(add) for add in adds))
    failed = [add for add in adds if add.problem]
    lines = [f"{len(adds) - len(failed)}곳 자동 수집 시작, {len(failed)}곳 못 찾음"]
    if skipped:
        lines.append(f"파일에서 건너뛴 줄 {skipped}개")
    lines.extend(f"못 찾음: {add.company} — {add.problem}" for add in failed[:20])
    await work_report.report("사이트 일괄 추가", lines, ok=not failed)


@router.get("/ui/sites/bulk", response_class=HTMLResponse)
def site_bulk_form(request: Request) -> HTMLResponse:
    return render(request, "fragments/site_bulk.html", step="form", max_rows=MAX_ROWS)


@router.get("/ui/sites/bulk/template")
def site_bulk_template() -> Response:
    """빈 양식. 첫 시트는 머리글만 두고, 칸 설명은 둘째 시트에 둔다 — 예시 줄이 등록되지 않게."""
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "사이트"
    sheet.append(list(TEMPLATE_HEADERS))
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    for column, width in zip("ABCD", (18, 50, 50, 10), strict=True):
        sheet.column_dimensions[column].width = width
    guide = book.create_sheet("설명")
    guide.append(["칸", "꼭", "설명"])
    guide.append(["회사 이름", "예", "공고의 회사명으로 고정됩니다. 그룹 채용이면 그룹 이름"])
    guide.append(["목록 페이지 주소", "예", "채용공고 여러 건이 보이는 목록 페이지 (https://…)"])
    guide.append(["예시 공고 주소", "", "공고 하나를 열었을 때 주소. 비우면 목록에서 찾습니다"])
    guide.append(["계열사", "", "계열사 공고가 함께 올라오는 그룹 사이트면 O"])
    guide.append(["", "", "첫 시트만 읽습니다. 이미 등록된 목록 주소는 건너뜁니다"])
    for column, width in zip("ABC", (18, 6, 60), strict=True):
        guide.column_dimensions[column].width = width
    out = io.BytesIO()
    book.save(out)
    return Response(
        out.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''site-bulk-template.xlsx"},
    )


@router.post("/ui/sites/bulk", response_class=HTMLResponse)
async def site_bulk_try(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
    generate: Annotated[crawlers.GenerateFn, Depends(crawlers.get_generator)],
    discover: Annotated[crawlers.DiscoverFn, Depends(crawlers.get_discoverer)],
    fetcher: Annotated[FetchPolicy, Depends(crawlers.get_crawl_fetcher)],
    scheduler: Annotated[WorkflowScheduler, Depends(workflows.get_workflow_scheduler)],
    repair: Annotated[crawlers.RepairFn, Depends(crawlers.get_repairer)],
    launch: Annotated[Callable[[Coroutine[Any, Any, None]], None], Depends(get_add_launcher)],
    connect: Annotated[Callable[[], sqlite3.Connection], Depends(get_add_connect)],
    sheet: Annotated[UploadFile | None, File()] = None,
) -> HTMLResponse:
    """파일의 줄마다 사이트 추가를 걸고 곧바로 돌아온다. 건너뛴 줄은 사유와 함께 남긴다."""

    def again(error: str) -> HTMLResponse:
        return render(
            request, "fragments/site_bulk.html", step="form", max_rows=MAX_ROWS, error=error
        )

    if sheet is None or not sheet.filename:
        return again("올릴 파일을 골라 주세요")
    data = await sheet.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        return again("파일이 2MB 를 넘어요. 사이트 목록만 남겨 다시 올려 주세요")
    try:
        rows = parse_rows(sheet.filename, data)
    except SheetError as exc:
        return again(str(exc))

    ready, skipped = sort_rows(rows, registered_urls(conn))
    adds = [
        site_adds.new(
            row.list_url,
            row.company,
            row.detail_url,
            DEFAULT_INTERVAL,
            has_affiliates=row.has_affiliates,
        )
        for row in ready
    ]
    if adds:

        def work(add: site_adds.SiteAdd) -> Coroutine[Any, Any, None]:
            return _work(add, connect, generate, discover, fetcher, scheduler, repair, report=False)

        launch(_all(adds, work, len(skipped)))
    logger.info("사이트 일괄 추가: %d곳을 걸고 %d줄을 건너뛰었다", len(ready), len(skipped))
    response = render(
        request, "fragments/site_bulk.html", step="queued", ready=ready, skipped=skipped
    )
    if ready:
        response.headers["HX-Trigger"] = "site-added"
    return response
