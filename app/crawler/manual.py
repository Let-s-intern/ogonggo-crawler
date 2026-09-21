"""공고 주소 하나를 넣으면 한 번만 가져와 저장하고 분류한다 (2026-09-21 결정).

사이트는 목록을 주기적으로 돈다. 그런데 목록이 없는 공고가 있다 — 동원 하반기 신입 공채는
캠페인 페이지 한 장이 공고 전부였고, 사이트로 등록할 방법이 없었다. 그런 공고를 운영자가 주소로
넣는 길이다.

| 단계 | 하는 일 |
|---|---|
| 받기 | 공용 fetch 클라이언트로 받는다. 글자가 거의 없으면 브라우저로 렌더한다 |
| 읽기 | 셀렉터 없이 페이지 본문을 편다. 메뉴·머리말·꼬리말은 뺀다 |
| 이미지 | 본문이 짧고 이미지가 있으면 읽는다. 공고 본문이 이미지뿐인 곳이 흔하다 |
| 저장 | 주기 수집과 같은 모양으로 `raw_jobs` 에 넣고 정규화한다 |
| 분류 | 그 공고 하나만 바로 분류한다. 결과는 공고 화면에서 검수하고 보낸다 |

## 셀렉터를 만들지 않는다

한 번 가져올 페이지라 셀렉터를 만들어 둘 이유가 없다. 분류가 원래 원문 전체를 읽고 칸을 나누므로
본문을 통째로 넘긴다. 제목도 분류가 짓는 공고 제목이 이긴다 (`app/normalize/engine.py`) — 페이지
제목은 사이트 공통 제목일 수 있어 대체값일 뿐이다.

## 워크플로우 하나에 모은다

`raw_jobs.workflow_id` 가 필수라 이렇게 넣은 공고도 워크플로우에 붙는다. 처음 넣을 때 `직접 추가`
워크플로우를 하나 만들고 그 뒤로 계속 쓴다. 사이트 목록과 스케줄러는 그것을 보지 않는다
(`migrations/0042_manual_workflow.sql`). 공고 화면에서는 사이트 이름이 `직접 추가` 로 보인다.

같은 주소를 두 번 넣으면 새로 저장하지 않고 이미 넣은 공고를 알려 준다. 다시 읽히려면 공고
화면의 `AI로 다시 채우기` 를 쓴다.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from app.crawler.fetcher import FetchError, FetchPolicy, PageSource
from app.crawler.hashing import content_hash
from app.crawler.images import ImageReader, read_detail_images
from app.crawler.parser import (
    PAGE_FURNITURE,
    DetailParseResult,
    ListItem,
    block_text,
    og_image,
    structured_text,
)
from app.crawler.runner import raw_record
from app.normalize.engine import NormalizeError, insert_normalized, load_rules
from app.selector.schema import DETAIL_FIELDS

logger = logging.getLogger(__name__)

MANUAL_KIND = "manual"
MANUAL_NAME = "직접 추가"

# 정적 HTML 에서 읽은 글자가 이보다 적으면 껍데기로 보고 렌더한다. 공고 본문이 이보다 짧은 페이지는
# 이미지 공고이고, 그때도 렌더해야 이미지 주소가 그려진다
MIN_TEXT_CHARS = 300
# 본문이 이보다 짧고 이미지가 있으면 이미지를 읽는다. 셀렉터 없이 페이지 전체를 읽어 메뉴 글자가
# 섞이므로 사이트 수집의 기준(500자)보다 넓다 — HD현대 공고 페이지가 이미지 한 장에 글자 수백 자다
IMAGE_READ_CHARS = 1500
# 공고 본문으로 먼저 보는 자리. 없으면 `body` 다
_CONTENT_SELECTORS = ("main", "article", "[role=main]")
# 공고 내용이 아닌 것. 스크립트와 SVG 아이콘, 입력 폼
_NOT_CONTENT = "script, style, noscript, template, svg, form, iframe, button"


class ManualAddError(ValueError):
    """넣을 수 없는 주소거나 가져오지 못했다. 사유를 화면이 그대로 옮긴다."""


@dataclass(frozen=True)
class ManualAdd:
    """직접 넣은 결과. `existed` 면 새로 저장하지 않았다."""

    raw_job_id: int
    workflow_id: int
    existed: bool = False
    notes: tuple[str, ...] = ()


# 렌더러를 여는 쪽. 브라우저는 정적으로 안 될 때만 뜬다
RenderOpener = Callable[[], Awaitable[PageSource]]


def manual_workflow(conn: sqlite3.Connection) -> int:
    """직접 넣은 공고를 담는 워크플로우 id. 없으면 크롤러와 함께 하나 만든다.

    크롤러의 목록 주소는 비워 둔다. 도는 목록이 없는 자리이고, 크롤러 목록에서도 빠진다
    (`app/api/ui_crawlers.py`). 워크플로우는 멈춘 채로 둔다 — 스케줄러가 `kind` 로 이미 거르지만,
    잘못 켜져도 돌 것이 없다.
    """
    row = conn.execute(
        "SELECT id FROM workflows WHERE kind = ? ORDER BY id LIMIT 1", (MANUAL_KIND,)
    ).fetchone()
    if row is not None:
        return int(row["id"])
    crawler = conn.execute(
        "INSERT INTO crawlers (name, list_url, status) VALUES (?, '', 'promoted')", (MANUAL_NAME,)
    )
    workflow = conn.execute(
        """
        INSERT INTO workflows (crawler_id, name, interval_minutes, status, kind)
        VALUES (?, ?, 1440, 'paused', ?)
        """,
        (crawler.lastrowid, MANUAL_NAME, MANUAL_KIND),
    )
    return int(workflow.lastrowid or 0)


async def add_posting(
    conn: sqlite3.Connection,
    url: str,
    *,
    company: str = "",
    fetcher: FetchPolicy,
    open_renderer: RenderOpener | None = None,
    reader: ImageReader | None = None,
) -> ManualAdd:
    """주소 하나를 받아 저장하고 정규화한다. 분류는 부르는 쪽이 한다 — 돌고 있는 분류와 겹치는지
    보는 일이 화면에 있다 (`app/api/ui_posting_add.py`)."""
    cleaned = url.strip()
    if urlsplit(cleaned).scheme not in ("http", "https") or not urlsplit(cleaned).netloc:
        raise ManualAddError(f"http:// 나 https:// 로 시작하는 공고 주소를 넣는다: {cleaned}")

    workflow_id = manual_workflow(conn)
    known = conn.execute(
        "SELECT id FROM raw_jobs WHERE workflow_id = ? AND source_url = ? ORDER BY id LIMIT 1",
        (workflow_id, cleaned),
    ).fetchone()
    if known is not None:
        return ManualAdd(raw_job_id=int(known["id"]), workflow_id=workflow_id, existed=True)

    html, page_url, notes = await _page(cleaned, fetcher, open_renderer)
    detail = page_detail(html)
    if reader is not None:
        detail = await read_detail_images(
            detail, page_url, fetcher, reader, short_chars=IMAGE_READ_CHARS
        )
        # 셀렉터로 본문을 가르지 않았으니 본문은 원문 전체다. 이미지 읽기는 본문이 비었을 때만
        # 본문을 바꾸는데, 제목 한 줄이 본문 영역에 있으면 읽은 글이 원문에만 붙고 본문은 제목으로
        # 남는다
        detail = replace(detail, fields={**detail.fields, "body": detail.source_text})
    if not detail.fields["body"].strip():
        raise ManualAddError(
            "페이지에서 공고 글자를 읽지 못했다. 공고 본문이 있는 주소인지 확인한다"
        )

    item = ListItem(
        index=0,
        title=detail.fields["title"],
        link=cleaned,
        date="",
        company_name=company.strip(),
    )
    record = raw_record(item, detail)
    cursor = conn.execute(
        """
        INSERT INTO raw_jobs (workflow_id, source_url, raw_data_json, content_hash)
        VALUES (?, ?, ?, ?)
        """,
        (workflow_id, cleaned, json.dumps(record, ensure_ascii=False), content_hash(record)),
    )
    raw_job_id = int(cursor.lastrowid or 0)
    try:
        insert_normalized(conn, raw_job_id, load_rules(conn))
    except NormalizeError as exc:
        # 원문은 남는다. 정규화 규칙을 고치고 다시 돌리면 된다
        notes = (*notes, f"정규화하지 못했다: {exc}")
    return ManualAdd(raw_job_id=raw_job_id, workflow_id=workflow_id, notes=(*notes, *detail.notes))


def page_detail(html: str) -> DetailParseResult:
    """셀렉터 없이 페이지를 공고 상세로 편다. 제목·본문·이미지·대표 이미지만 채운다."""
    soup = BeautifulSoup(html, "html.parser")
    title = _title(soup)
    cover = og_image(soup)
    for node in soup.select(f"{_NOT_CONTENT}, {PAGE_FURNITURE}"):
        node.decompose()
    container = _container(soup)
    text = block_text(container)
    images = _images(container)
    structured = structured_text(soup, text) if text.strip() else ""
    fields = {name: "" for name in DETAIL_FIELDS}
    fields["title"] = title
    fields["body"] = text
    return DetailParseResult(
        fields=fields,
        missing=[],
        source_text=f"{text}\n{structured}" if structured else text,
        images=images,
        cover_image=cover,
    )


async def _page(
    url: str, fetcher: FetchPolicy, open_renderer: RenderOpener | None
) -> tuple[str, str, tuple[str, ...]]:
    """정적으로 받아 보고, 글자가 모자라면 렌더한다. 렌더할 수 없으면 정적 결과로 간다."""
    try:
        page = await fetcher.fetch(url)
    except FetchError as exc:
        raise ManualAddError(f"주소를 열지 못했다: {exc}") from exc
    if _text_length(page.text) >= MIN_TEXT_CHARS or open_renderer is None:
        return page.text, page.url, ()
    try:
        renderer = await open_renderer()
        rendered = await renderer.fetch(url)
    except FetchError as exc:
        logger.info("직접 추가: 렌더하지 못해 정적 HTML 로 간다 url=%s: %s", url, exc)
        return page.text, page.url, (f"브라우저로 열지 못해 정적 HTML 만 읽었다: {exc}",)
    return rendered.text, rendered.url, ("정적 HTML 에 글자가 거의 없어 브라우저로 열었다",)


def _text_length(html: str) -> int:
    detail = page_detail(html)
    return len(" ".join(detail.fields["body"].split()))


def _container(soup: BeautifulSoup) -> Tag:
    """공고 본문으로 볼 노드. `main`·`article` 이 충분히 글자를 가지면 그것, 아니면 `body` 다."""
    for selector in _CONTENT_SELECTORS:
        node = soup.select_one(selector)
        if node is not None and len(" ".join(node.get_text().split())) >= MIN_TEXT_CHARS:
            return node
    return soup.body if soup.body is not None else soup


def _title(soup: BeautifulSoup) -> str:
    """페이지 제목. 본문 첫 `h1`, `og:title`, `<title>` 순이다. 분류가 짓는 제목의 대체값이다."""
    heading = soup.select_one("h1")
    if heading is not None and heading.get_text(strip=True):
        return " ".join(heading.get_text().split())
    meta = soup.find("meta", attrs={"property": "og:title"})
    if isinstance(meta, Tag):
        content = str(meta.get("content") or "").strip()
        if content:
            return content
    return " ".join(soup.title.get_text().split()) if soup.title is not None else ""


def _images(container: Tag) -> tuple[str, ...]:
    """본문 안 이미지 주소. `data:` 와 SVG 는 뺀다.

    둘 다 대개 아이콘이고, 이미지 읽기가 SVG 를 열지 못해 한 장이 섞이면 전부 실패한다.
    """
    found: dict[str, None] = {}
    for image in container.find_all("img"):
        raw = image.get("src") or image.get("data-src") or ""
        source = (" ".join(raw) if isinstance(raw, list) else str(raw)).strip()
        if not source or source.startswith("data:"):
            continue
        if urlsplit(source).path.lower().endswith(".svg"):
            continue
        found[source] = None
    return tuple(found)
