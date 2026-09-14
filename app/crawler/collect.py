"""실행 하나가 목록과 상세를 무엇으로 가져올지 고르는 자리.

`crawlers.list_mode` 와 `detail_mode` 가 각각 `static` / `api` / `playwright` 중 하나다.
**섞어 쓰는 것이 정상적인 선택지다** — 목록이 JSON API 로 오고 상세는 브라우저가 있어야
그려지는 사이트가 있고, 하나의 값으로는 그 사이트를 담을 수 없다.

여기서 고른 뒤로는 러너가 같은 코드를 돈다. 수집기는 둘 다 같은 것을 돌려준다.

| 수집기 | 돌려주는 것 |
|---|---|
| `ListCollector.collect()` | `ListParseResult` |
| `DetailCollector.collect(item)` | `DetailParseResult` |

## 브라우저는 실제로 필요한 쪽에서만 뜬다

`open_collectors()` 가 브라우저의 수명을 들고 있다. 목록이 `api` 면 목록 때문에 브라우저가
뜨지 않고, 상세만 `playwright` 면 상세를 가져올 때 하나가 뜬다. 양쪽이 다 `playwright` 면
브라우저 하나를 나눠 쓴다 — 인스턴스 하나가 150~300MB 다 (`.claude/rules/crawling.md`).

셀렉터 생성과 AI 수정은 아직 `app/crawler/playwright.py` 의 `open_source()` 를 쓴다. 그쪽은
HTML 한 장을 가져오는 일이라 목록·상세를 나눌 자리가 없다.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from app.crawler import playwright as render_module
from app.crawler.api_source import fetch_detail, fetch_list
from app.crawler.fetcher import FetchPolicy, PageSource
from app.crawler.images import ImageReader, read_detail_images
from app.crawler.parser import (
    DetailParseResult,
    FieldParseError,
    ListItem,
    ListParseResult,
    SelectorMissError,
    parse_detail,
    parse_list,
)
from app.crawler.playwright import PLAYWRIGHT, STATIC, Renderer
from app.crawler.shell import promotion_hint
from app.selector.api_schema import ApiConfig
from app.selector.schema import DetailSelectors, ListSelectors, SelectorSet

logger = logging.getLogger(__name__)

API = "api"
# `crawlers.list_mode` 와 `detail_mode` 의 CHECK 제약과 같은 값이어야 한다
COLLECT_MODES: tuple[str, ...] = (STATIC, API, PLAYWRIGHT)


class ListCollector(Protocol):
    async def collect(self) -> ListParseResult: ...


class DetailCollector(Protocol):
    async def collect(self, item: ListItem) -> DetailParseResult: ...


@dataclass(frozen=True)
class Collectors:
    """이 실행이 쓸 두 수집기. 모드는 실패 사유에 적으려고 함께 들고 있다."""

    list_mode: str
    detail_mode: str
    list: ListCollector
    detail: DetailCollector
    # 목록에서 읽은 날짜가 그 공고의 마감일인가. 참이면 러너가 마감이 지난 공고의 상세를
    # 열지 않는다 (`app/crawler/runner.py`)
    list_date_is_deadline: bool = False


class HtmlListCollector:
    """HTML 목록. 정적 fetch 와 렌더가 같은 코드를 쓰고 `source` 만 다르다."""

    def __init__(
        self, source: PageSource, list_url: str, selectors: ListSelectors, mode: str
    ) -> None:
        self._source = source
        self._list_url = list_url
        self._selectors = selectors
        self._mode = mode

    async def collect(self) -> ListParseResult:
        page = await self._source.fetch(self._list_url)
        try:
            return parse_list(page.text, self._selectors, page.url)
        except SelectorMissError as exc:
            # 0개 매칭이 마크업 변경인지 JS 렌더인지를 사유에 적는다. 승격은 운영자가 정하므로
            # 여기서 모드를 바꾸지 않는다 (`app/crawler/shell.py`)
            hint = promotion_hint(page.text, self._mode)
            if hint is None:
                raise
            raise SelectorMissError(f"{exc}. {hint}") from exc


class HtmlDetailCollector:
    """HTML 상세. 항목의 링크를 그대로 따라간다.

    `reader` 가 있으면 본문이 짧고 원문 영역에 이미지가 있는 공고의 이미지를 읽어 원문에 붙인다.
    이미지는 렌더러가 아니라 `fetcher` 로 받는다 — 바이트를 받을 수 있는 쪽이 그것뿐이다
    (`app/crawler/images.py`).
    """

    def __init__(
        self,
        source: PageSource,
        selectors: DetailSelectors,
        *,
        fetcher: FetchPolicy | None = None,
        reader: ImageReader | None = None,
    ) -> None:
        self._source = source
        self._selectors = selectors
        self._fetcher = fetcher
        self._reader = reader

    async def collect(self, item: ListItem) -> DetailParseResult:
        page = await self._source.fetch(item.link)
        detail = parse_detail(page.text, self._selectors)
        if self._fetcher is None or self._reader is None:
            return detail
        return await read_detail_images(detail, page.url, self._fetcher, self._reader)


class ApiListCollector:
    """JSON 목록. 요청은 공용 fetch 클라이언트로 나간다.

    `known` 은 "이 주소는 이미 담은 공고인가" 다. 쪽이 통째로 아는 공고면 다음 쪽을 받지
    않는다 — 목록이 새것부터 오므로 그 뒤는 더 옛것이다. 주지 않으면 끝까지 넘긴다.
    """

    def __init__(
        self,
        client: FetchPolicy,
        config: ApiConfig,
        known: Callable[[str], bool] | None = None,
    ) -> None:
        self._client = client
        self._config = config.list_config()
        self._known = known

    async def collect(self) -> ListParseResult:
        return await fetch_list(self._client, self._config, known=self._known)


class ApiDetailCollector:
    """JSON 상세. 공고 id 로 한 건을 지목해 가져온다.

    id 는 목록이 API 면 응답에서 그대로 오고(`ListItem.detail_key`), 목록이 HTML 이면 상세
    링크의 마지막 경로 조각을 쓴다. 그것도 없으면 무엇을 물어볼지 알 수 없으므로 실패다 —
    아무 값이나 넣어 엉뚱한 공고를 가져오지 않는다.
    """

    def __init__(self, client: FetchPolicy, config: ApiConfig) -> None:
        self._client = client
        self._config = config.detail_config()

    async def collect(self, item: ListItem) -> DetailParseResult:
        item_id = item.detail_key or _id_from_link(item.link)
        if not item_id:
            raise FieldParseError(
                f"상세 API 에 넘길 id 를 찾지 못했다: {item.link}. "
                "목록을 `api` 로 두거나 링크 마지막 조각이 id 인 사이트여야 한다"
            )
        return await fetch_detail(self._client, self._config, item_id)


@asynccontextmanager
async def open_collectors(
    *,
    list_mode: str,
    detail_mode: str,
    list_url: str,
    selectors: SelectorSet,
    fetcher: FetchPolicy,
    api_config: ApiConfig | None = None,
    renderer: Callable[[FetchPolicy], Renderer] | None = None,
    known: Callable[[str], bool] | None = None,
    image_reader: ImageReader | None = None,
) -> AsyncIterator[Collectors]:
    """이 실행이 쓸 수집기 둘을 만든다. 브라우저 수명이 이 블록이다.

    `image_reader` 를 주면 HTML 상세에서 본문이 이미지로만 올라온 공고의 이미지를 읽는다
    (`app/crawler/images.py`). 주지 않으면 읽지 않는다.

    모르는 모드는 정적이다. 렌더도 API 도 운영자가 명시적으로 올린 사이트만 받는다.
    """
    # 모듈을 통해 부른다. 렌더러를 대역으로 바꿔 끼우는 시험이 `open_source()` 와 같은 자리를
    # 본다 — 브라우저가 뜨는 경로가 둘로 갈리면 어느 쪽이 막혔는지 알 수 없다
    build = renderer or (lambda client: render_module.Renderer(client))
    instance: Renderer | None = None

    def source_for(mode: str) -> PageSource:
        nonlocal instance
        if mode != PLAYWRIGHT:
            return fetcher
        if instance is None:
            # 여기까지 와야 브라우저가 뜬다. 목록이 api 면 목록 때문에 뜨는 일은 없다
            instance = build(fetcher)
            logger.info("렌더러를 띄운다 list_mode=%s detail_mode=%s", list_mode, detail_mode)
        return instance

    config = api_config or ApiConfig()
    try:
        list_collector: ListCollector = (
            ApiListCollector(fetcher, config, known)
            if list_mode == API
            else HtmlListCollector(source_for(list_mode), list_url, selectors.list, list_mode)
        )
        detail_collector: DetailCollector = (
            ApiDetailCollector(fetcher, config)
            if detail_mode == API
            else HtmlDetailCollector(
                source_for(detail_mode), selectors.detail, fetcher=fetcher, reader=image_reader
            )
        )
        yield Collectors(
            list_mode=list_mode,
            detail_mode=detail_mode,
            list=list_collector,
            detail=detail_collector,
            list_date_is_deadline=list_date_is_deadline(list_mode, config, selectors),
        )
    finally:
        if instance is not None:
            await instance.aclose()


def list_date_is_deadline(list_mode: str, config: ApiConfig, selectors: SelectorSet) -> bool:
    """목록에서 읽은 날짜가 그대로 마감일이 되는 크롤러인가.

    `list.date` 는 사이트가 목록에 적어 둔 날짜일 뿐이고, 그것이 마감일인지 게시일인지는
    사이트마다 다르다. 게시일을 마감일로 읽으면 어제 올라온 새 공고를 지난 공고로 버린다.

    목록이 API 면 설정이 말해 준다 (`list.date_is_deadline`). 응답 필드를 보고 사람이 적은
    값이고, 적지 않았으면 건너뛰지 않는다 — 모르는 쪽은 열어 본다.

    목록이 HTML 이면 상세에 마감일 셀렉터가 없을 때만 목록 날짜를 마감일로 쓴다. `_record()`
    가 그때만 목록 날짜를 마감일 자리에 넣기 때문이고, 두 자리의 판정이 갈리면 화면에 보이는
    마감일과 건너뛴 이유가 어긋난다.
    """
    if list_mode == API:
        return config.list is not None and config.list.date_is_deadline
    return not selectors.detail.recruitment_end_at.strip()


def html_collectors(
    source: PageSource, list_url: str, selectors: SelectorSet, mode: str = STATIC
) -> Collectors:
    """HTML 한 경로로만 도는 수집기. 브라우저를 여기서 띄우지 않는다.

    이미 열려 있는 `PageSource` 를 그대로 쓰는 자리가 있다 — 렌더러를 밖에서 열어 둔 호출과,
    가짜 소스를 끼우는 시험이다. 어느 쪽이든 수명은 부르는 쪽이 들고 있다.
    """
    return Collectors(
        list_mode=mode,
        detail_mode=mode,
        list=HtmlListCollector(source, list_url, selectors.list, mode),
        detail=HtmlDetailCollector(source, selectors.detail),
        list_date_is_deadline=list_date_is_deadline(mode, ApiConfig(), selectors),
    )


def _id_from_link(link: str) -> str:
    """상세 링크의 마지막 경로 조각. 목록이 HTML 이고 상세가 API 인 사이트가 쓴다.

    id 를 쿼리 문자열에 두는 사이트는 여기서 다루지 않는다. 그런 사이트가 나오면 무엇을
    id 로 볼지 설정에 적게 하는 것이 맞고, 지금 추측으로 고르면 조용히 틀린 공고를 가져온다.
    """
    path = urlsplit(link).path.rstrip("/")
    return path.rsplit("/", 1)[-1] if "/" in path else ""
