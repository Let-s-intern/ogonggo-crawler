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

## 로고 (2026-11-09)

로고는 파일이 아니라 `로고 주소` 칸으로 받는다. 파일 칸을 줄마다 둘 수 없고, 엑셀 칸 안에 붙인
그림은 openpyxl 이 읽지 못하거나 어느 줄 것인지 위치로 짐작해야 한다. 주소를 그대로 저장하지는
않는다 — 남의 주소는 언제 사라질지 모른다(`ui_site_add` 의 로고 결정). 공용 fetch 클라이언트로
내려받아 한 건 추가와 같은 길로 저장소에 올린다 (`ui_companies.save_logo_bytes`).

엑셀을 채우는 사람은 회사가 아직 없어 로고를 올릴 곳이 없다. 그래서 `로고 올려 주소 받기`
(`/ui/sites/bulk/logos`)로 이미지만 저장소에 올리고 주소를 받아 칸에 적는다 (2026-11-09). 그 주소는
우리 저장소라 다시 내려받지 않고 그대로 회사에 붙인다.

받기는 요청 밖에서 사이트 추가 바로 앞에 한다. 로고를 못 받아도 사이트 추가는 그대로 건다 — 로고는
나중에 사이트 패널에서 올리면 되지만 사이트는 다시 넣어야 한다. 못 받은 로고는 끝에 보내는 슬랙
한 통에 회사와 사유가 적힌다. 주소 꼴이 틀렸거나 저장소가 비어 있으면 걸 때 창에 바로 적는다.

## 슬랙 보고 (2026-11-09)

다 끝나면 한 통으로 넣은 수 · 성공 · 실패 · 중복과 실패 목록을 보낸다. 성공은 시험 수집이 되어
자동 수집까지 시작한 곳이다. 실패는 입력이 틀려 걸지 못한 줄과, 걸었지만 등록·시험 수집에서
막힌 곳을 합친다. 중복은 이미 등록됐거나 파일 안에서 겹친 목록 주소다. 하나도 걸지 못한
파일이어도 보낸다 — 올린 사람이 결과를 슬랙에서 본다.

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
from app.api.ui_companies import assign_logo, is_uploaded_logo, save_logo_bytes, upload_logo
from app.api.ui_site_add import (
    DEFAULT_INTERVAL,
    _work,
    check_input,
    get_add_connect,
    get_add_launcher,
)
from app.crawler.fetcher import FetchError, FetchPolicy
from app.notify import work_report
from app.scheduler import WorkflowScheduler
from app.storage import s3
from app.storage import settings as store

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ui"], include_in_schema=False)

# 한 번에 올릴 수 있는 줄 수. 줄마다 AI 로 셀렉터를 만든다 — 실수로 큰 파일을 올려 비용이 새지 않게
MAX_ROWS = 200
MAX_BYTES = 2 * 1024 * 1024

COMPANY = "company"
LIST_URL = "list_url"
DETAIL_URL = "detail_url"
AFFILIATES = "has_affiliates"
LOGO_URL = "logo_url"

# 머리글 이름 → 칸. 띄어쓰기는 지우고 비교한다. 양식의 이름이 맨 앞이다
HEADERS: dict[str, tuple[str, ...]] = {
    COMPANY: ("회사이름", "회사", "회사명", "그룹이름"),
    LIST_URL: ("목록페이지주소", "목록주소", "목록url", "채용목록주소"),
    DETAIL_URL: ("예시공고주소", "공고주소", "상세주소", "상세url"),
    AFFILIATES: ("계열사", "계열사여부", "그룹채용"),
    LOGO_URL: ("로고주소", "로고", "로고url", "로고이미지주소", "로고이미지"),
}
TEMPLATE_HEADERS = ("회사 이름", "목록 페이지 주소", "예시 공고 주소", "계열사", "로고 주소")

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
    logo_url: str = ""


# 건너뛴 줄의 갈래. 슬랙 보고가 중복과 실패를 따로 센다
INVALID = "invalid"
DUPLICATE = "duplicate"


@dataclass
class Skipped:
    line: int
    label: str
    reason: str
    kind: str = INVALID


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
            logo_url=at(row, LOGO_URL),
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
        if problem:
            skipped.append(Skipped(row.line, label, problem))
            continue
        if _same_url(row.list_url) in seen:
            reason = (
                "이미 등록됐거나 추가 중인 목록 주소예요"
                if _same_url(row.list_url) in taken
                else "이 파일의 앞 줄과 목록 주소가 같아요"
            )
            skipped.append(Skipped(row.line, label, reason, DUPLICATE))
            continue
        seen.add(_same_url(row.list_url))
        ready.append(row)
    return ready, skipped


def check_logos(ready: list[Row], storage_ready: bool) -> list[Skipped]:
    """로고 주소를 쓸 수 없는 줄. 사이트는 그대로 걸고 로고 주소만 비운다."""
    notes: list[Skipped] = []
    for row in ready:
        if not row.logo_url:
            continue
        if not storage_ready:
            reason = "파일 저장소가 비어 있어 로고는 올리지 않았어요(설정 › 파일 저장소)"
        elif not row.logo_url.startswith(("http://", "https://")):
            reason = "로고 주소가 http:// 나 https:// 로 시작하지 않아 로고 없이 걸었어요"
        else:
            continue
        notes.append(Skipped(row.line, row.company, reason))
        row.logo_url = ""
    return notes


async def put_logo(
    connect: Callable[[], sqlite3.Connection], fetcher: FetchPolicy, company: str, url: str
) -> str:
    """로고 주소에서 받아 저장소에 올린다. 못 하면 사유를, 되면 빈 문자열을 돌려준다.

    `로고 올려 주소 받기` 로 받은 우리 저장소 주소면 다시 받지 않고 그대로 붙인다.
    """
    conn = connect()
    try:
        if is_uploaded_logo(conn, url):
            assign_logo(conn, company, url)
            return ""
    finally:
        conn.close()
    try:
        result = await fetcher.request(url)
    except FetchError as exc:
        return f"로고를 받지 못했어요: {exc}"
    if len(result.content) > s3.MAX_IMAGE_BYTES:
        return f"로고가 {s3.MAX_IMAGE_LABEL} 를 넘어요"
    conn = connect()
    try:
        save_logo_bytes(conn, company, result.content)
    except s3.StorageError as exc:
        return f"로고를 올리지 못했어요: {exc.message}"
    finally:
        conn.close()
    return ""


@dataclass
class Batch:
    """파일 한 장의 결과. 다 끝나면 슬랙 한 통으로 정리한다 (`summary_lines`)."""

    filename: str
    total: int
    skipped: list[Skipped]
    adds: list[site_adds.SiteAdd]
    lines: dict[int, int]
    logo_problems: dict[int, str]


# 슬랙 실패 목록에 적는 최대 수. 넘으면 몇 곳 더 있는지만 적는다
MAX_LISTED = 50


def summary_lines(batch: Batch) -> list[str]:
    """넣음 · 성공 · 실패 · 중복과 실패 목록. 실패는 입력 오류와 등록·시험 수집 실패를 합친다."""
    duplicates = [item for item in batch.skipped if item.kind == DUPLICATE]
    invalid = [item for item in batch.skipped if item.kind != DUPLICATE]
    failed_adds = [add for add in batch.adds if add.problem]
    succeeded = len(batch.adds) - len(failed_adds)
    failures = [
        *((item.line, item.label, item.reason) for item in invalid),
        *((batch.lines[add.id], add.company, add.problem) for add in failed_adds),
    ]
    failures.sort()
    lines = [
        f"파일: {batch.filename}",
        f"넣음 {batch.total}곳",
        f"성공 {succeeded}곳 (시험 수집이 되어 자동 수집 시작)",
        f"실패 {len(failures)}곳 (입력 오류 {len(invalid)},"
        f" 등록·시험 수집 실패 {len(failed_adds)})",
        f"중복 {len(duplicates)}곳",
    ]
    if failures:
        lines.append("실패 목록")
        lines.extend(
            f"    {line}번째 줄 {label} — {reason}" for line, label, reason in failures[:MAX_LISTED]
        )
        if len(failures) > MAX_LISTED:
            lines.append(f"    외 {len(failures) - MAX_LISTED}곳")
    problems = [
        (add, batch.logo_problems[add.id]) for add in batch.adds if add.id in batch.logo_problems
    ]
    if problems:
        lines.append(f"로고 못 올림 {len(problems)}곳 (사이트는 그대로 추가)")
        lines.extend(f"    {add.company} — {problem}" for add, problem in problems[:MAX_LISTED])
    return lines


async def _all(
    batch: Batch,
    work: Callable[[site_adds.SiteAdd], Coroutine[Any, Any, None]],
    logo: Callable[[site_adds.SiteAdd], Coroutine[Any, Any, str]],
) -> None:
    """한꺼번에 걸고 다 끝나면 슬랙으로 한 통 보낸다. 동시에 도는 수는 `site_adds.slot` 이
    막는다. 로고는 사이트 추가 바로 앞에 받는다.
    """

    async def one(add: site_adds.SiteAdd) -> None:
        try:
            problem = await logo(add)
        except Exception as exc:  # noqa: BLE001 — 로고 때문에 사이트 추가가 멈추면 안 된다
            logger.exception("사이트 일괄 추가: %s 로고에서 예상하지 못한 오류", add.company)
            problem = f"로고를 올리지 못했어요: {type(exc).__name__}"
        if problem:
            batch.logo_problems[add.id] = problem
            logger.info("사이트 일괄 추가: %s %s", add.company, problem)
        await work(add)

    await asyncio.gather(*(one(add) for add in batch.adds))
    ok = all(not add.problem for add in batch.adds) and not batch.skipped
    await work_report.report("사이트 일괄 추가", summary_lines(batch), ok=ok)


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
    for column, width in zip("ABCDE", (18, 50, 50, 10, 50), strict=True):
        sheet.column_dimensions[column].width = width
    guide = book.create_sheet("설명")
    guide.append(["칸", "꼭", "설명"])
    guide.append(["회사 이름", "예", "공고의 회사명으로 고정됩니다. 그룹 채용이면 그룹 이름"])
    guide.append(["목록 페이지 주소", "예", "채용공고 여러 건이 보이는 목록 페이지 (https://…)"])
    guide.append(["예시 공고 주소", "", "공고 하나를 열었을 때 주소. 비우면 목록에서 찾습니다"])
    guide.append(["계열사", "", "계열사 공고가 함께 올라오는 그룹 사이트면 O"])
    guide.append(
        [
            "로고 주소",
            "",
            "사이트 추가 › 엑셀 › '로고 올려 주소 받기'에서 받은 주소. 다른 이미지 주소도 되고,"
            " 서버가 내려받아 보관합니다. 비우면 사이트 아이콘",
        ]
    )
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
    notes = check_logos(ready, store.read_config(conn).configured)
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
    logos = {add.id: row.logo_url for add, row in zip(adds, ready, strict=True)}

    def work(add: site_adds.SiteAdd) -> Coroutine[Any, Any, None]:
        return _work(add, connect, generate, discover, fetcher, scheduler, repair, report=False)

    async def logo(add: site_adds.SiteAdd) -> str:
        url = logos[add.id]
        return await put_logo(connect, fetcher, add.company, url) if url else ""

    batch = Batch(
        filename=sheet.filename,
        total=len(rows),
        skipped=skipped,
        adds=adds,
        lines={add.id: row.line for add, row in zip(adds, ready, strict=True)},
        logo_problems={},
    )
    # 하나도 걸지 못했어도 결과는 슬랙으로 간다
    launch(_all(batch, work, logo))
    logger.info("사이트 일괄 추가: %d곳을 걸고 %d줄을 건너뛰었다", len(ready), len(skipped))
    response = render(
        request,
        "fragments/site_bulk.html",
        step="queued",
        ready=ready,
        skipped=skipped,
        notes=notes,
        logo_count=sum(1 for row in ready if row.logo_url),
    )
    if ready:
        response.headers["HX-Trigger"] = "site-added"
    return response


# `로고 올려 주소 받기` 에서 한 번에 올릴 수 있는 이미지 수
MAX_LOGOS = 50


@dataclass
class UploadedLogo:
    filename: str
    url: str = ""
    problem: str = ""


@router.get("/ui/sites/bulk/logos", response_class=HTMLResponse)
def site_bulk_logos_form(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
) -> HTMLResponse:
    """로고 이미지만 저장소에 올려 주소를 받는 창. 받은 주소를 엑셀의 `로고 주소` 칸에 적는다."""
    return _logos(request, conn, step="form")


def _logos(request: Request, conn: sqlite3.Connection, **context: object) -> HTMLResponse:
    return render(
        request,
        "fragments/site_bulk_logos.html",
        storage_ready=store.read_config(conn).configured,
        accept_attr=s3.ACCEPT_ATTR,
        accepted=s3.ACCEPTED,
        max_label=s3.MAX_IMAGE_LABEL,
        max_logos=MAX_LOGOS,
        **context,
    )


@router.post("/ui/sites/bulk/logos", response_class=HTMLResponse)
async def site_bulk_logos_upload(
    request: Request,
    conn: Annotated[sqlite3.Connection, Depends(crawlers.get_connection)],
    logos: Annotated[list[UploadFile] | None, File()] = None,
) -> HTMLResponse:
    """고른 이미지를 하나씩 올린다. 한 장이 안 돼도 나머지는 올리고, 안 된 것은 사유를 적는다."""
    files = [item for item in logos or [] if item.filename]
    if not files:
        return _logos(request, conn, step="form", error="올릴 이미지를 골라 주세요")
    if len(files) > MAX_LOGOS:
        return _logos(request, conn, step="form", error=f"한 번에 {MAX_LOGOS}장까지 올릴 수 있어요")
    results: list[UploadedLogo] = []
    for item in files:
        filename = item.filename or ""
        data = await item.read(s3.MAX_IMAGE_BYTES + 1)
        try:
            results.append(UploadedLogo(filename, url=upload_logo(conn, data)))
        except s3.StorageError as exc:
            results.append(UploadedLogo(filename, problem=exc.message))
    logger.info(
        "로고 주소 받기: %d장 중 %d장 올렸다", len(results), sum(1 for r in results if r.url)
    )
    return _logos(request, conn, step="done", results=results)
