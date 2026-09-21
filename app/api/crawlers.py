"""크롤러 등록과 셀렉터 수동 보정.

등록은 리스트 URL 과 상세 URL 을 받아 셀렉터를 생성하고 `crawlers` 행을 `status=draft` 로
남기는 데까지다. 여기서 워크플로우가 되지는 않는다 — 테스트 실행을 거쳐야 `tested` 가 되고,
그 다음이 승격이다 (`.claude/docs/data-model.md`).

생성된 셀렉터는 가설이라 실패한 필드가 있어도 행은 남는다. 실패한 필드 이름을 응답에 실어
운영자가 그 필드만 손으로 고치게 한다. 손으로 고친 셀렉터를 요청 없이 다시 생성하지 않는다
(`.claude/rules/llm.md`).

예외는 둘이다. 목록 필드가 전부 0개 매칭이면 정적 HTML 에 목록이 없는 것이고, 항목은 잡혔는데
그 안의 제목·링크·날짜가 전부 0개면 항목 셀렉터가 목록이 아닌 다른 것을 잡은 것이다. 둘 다 행을
남기지 않고 실패로 돌려주되 사유를 갈라 적는다 — 앞은 렌더 승격을, 뒤는 항목 셀렉터를 다시
잡는 것을 다음 수단으로 가리킨다. 어느 쪽이든 저장해 봐야 아무것도 뽑지 못하는 크롤러가 남고,
0건 추출을 성공으로 내보내지 않는다는 규칙이 생성 단계에도 적용된다.

테스트 실행은 저장된 셀렉터로 실제 페이지를 1회 크롤링해 필드별 미리보기와 실패 사유를
돌려준다. 통과한 것만 `tested` 가 된다 — 실패한 실행은 상태를 건드리지 않는다.

삭제는 크롤러 정의만 지운다. 워크플로우로 승격된 크롤러는 거절한다 — 워크플로우와 그 실행
기록이 매달려 있고, 정의만 사라지면 남은 기록이 누구 것인지 아무도 설명하지 못한다. 수집한
데이터(`raw_jobs`, `normalized_jobs`)는 크롤러 정의와 수명이 다르므로 함께 지우지 않는다
(`.claude/rules/data-safety.md`).

상세 URL 은 선택이다. 상세를 JS 로 그려서 공고마다 주소가 따로 없는 사이트가 있고, 그런
사이트에 없는 주소를 지어내 가져오지 않는다. 비우면 목록 페이지만 보고 생성하며, 상세
셀렉터는 확인하지 않은 가설로 남는다 — 실패가 아니라 건너뛴 것이라 응답에서 갈라 적는다.

`default_company` 는 **모회사 이름이다** (2026-08-29 결정 전에는 "회사명이 페이지에 없는
사이트를 위한 선택 입력"이었다). 운영자가 타이핑한 값이라 추출 결과가 아니고, 그래서
`crawlers` 에만 있고 `raw_jobs` 에는 가지 않는다 (`.claude/rules/data-safety.md`). 정규화의
`parent_company_name` 가 이 값을 그대로 옮긴다(`app/normalize/engine.py` 의 `read_parent_company`) —
비어 있으면 크롤러 이름을 대신 쓰던 옛 동작은 더 이상 없다.

**등록 화면은 이 칸을 필수로 받는다.** `app/api/ui_crawlers.py` 의 `create_crawler_fragment`
가 폼 제출을 걸러낸다 — 모회사가 운영자가 적은 값인지 시스템이 짐작한 값인지 화면에서 갈리지
않게 하려는 것이다. 사이트가 계열사를 하나만 갖고 있으면(토스·우아한형제들처럼) 그 크롤러
이름을 그대로 적으면 되고, 그것도 운영자가 정한 값이다.

**이 파일의 `create_crawler`/`update_company` 자체는 막지 않는다.** 막으면 이 함수를 그대로
쓰는 기존 테스트·가져오기 경로가 전부 깨진다. 필수 검사는 사람이 마주치는 등록 화면 한 곳에
있다. 이 결정 전에 등록돼 비어 있던 행은 `migrations/0022_backfill_default_company.sql` 이
한 번 채웠다.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import replace
from typing import Annotated, Any, Protocol
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app import db
from app.config import Settings
from app.crawler.collect import API, COLLECT_MODES, open_collectors
from app.crawler.failures import SUCCESS
from app.crawler.fetcher import FetchError, FetchPolicy, RobotsDisallowedError, get_fetcher
from app.crawler.images import LlmImageReader
from app.crawler.playwright import PLAYWRIGHT, RENDER_MODES, STATIC, Renderer, open_source
from app.crawler.runner import TEST, RunTarget, collect_selectors, run_once
from app.llm import settings as llm_settings
from app.llm.log import SELECTOR_GENERATE, SELECTOR_REPAIR, record_call
from app.selector.api_schema import (
    ApiConfig,
    ApiConfigError,
    parse_api_config,
    validate_api_config,
)
from app.selector.detail_path import DOCUMENT
from app.selector.discovery import Discovery, discover_detail_path
from app.selector.generator import (
    GenerationResult,
    SelectorGenerationError,
    Usage,
    generate_for_urls,
    generate_from_html,
)
from app.selector.repair import RepairOutcome, SelectorRepairError, repair_for_urls
from app.selector.schema import SelectorSchemaError, SelectorSet, validate_selectors
from app.selector.verify import VerificationReport

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/crawlers", tags=["crawlers"])

# 새 크롤러가 받는 모드. 정적이다 — 브라우저 하나가 실행당 150~300MB 이고 정적은 사실상
# 0이라, 정적으로 되는 사이트까지 렌더로 돌리면 그만큼이 그냥 나간다. 측정한 사이트 6개 중
# 4개가 JS 렌더지만, 어느 쪽이 필요한지는 테스트 실행 화면에서 두 모드를 비교해 정한다
DEFAULT_RENDER_MODE = STATIC

# 인자는 리스트 URL, 상세 URL, render_mode 다. 어느 경로로 가져올지는 크롤러마다 다르므로
# 생성 함수가 매번 받는다.
GenerateFn = Callable[[str, str, str], Awaitable[GenerationResult]]

# 등록할 때 상세로 가는 길을 찾는 경로. 인자는 리스트 URL 과 방금 만든 셀렉터다
# (`app/selector/discovery.py`). 판정은 제안이고 저장 여부는 이 라우트가 정한다.
DiscoverFn = Callable[[str, SelectorSet], Awaitable[Discovery]]


class RepairFn(Protocol):
    """고치기 경로. 저장된 셀렉터를 기준으로만 돈다 — 무엇이 이미 맞는지 알아야 피해서 고른다.

    `hint` 는 운영자가 브라우저에서 보고 준 단서다. 비어 있으면 힌트가 생기기 전과 같은
    프롬프트로 돈다 (`app/selector/repair.py`). 키워드 인자인 것은 앞의 넷이 어디서 왔는지
    (DB 행) 와 이것이 어디서 왔는지(화면의 입력칸)가 다르기 때문이다.
    """

    async def __call__(
        self,
        list_url: str,
        detail_url: str,
        render_mode: str,
        selectors: SelectorSet,
        *,
        hint: str = "",
    ) -> RepairOutcome: ...


class CrawlerCreate(BaseModel):
    list_url: str
    # 선택이다. 상세를 JS 로 그려 주소가 따로 없는 사이트가 있다
    detail_url: str = ""
    name: str = ""
    # 모회사 이름. 등록 화면이 필수로 받지만 이 필드 자체는 비울 수 있다 — 화면 밖 호출부
    # (테스트, 가져오기)까지 막으면 파급이 너무 크다
    default_company: str = ""
    # 비우는 것이 기본이고, 그때는 등록이 스스로 정한다 — 정적으로 목록이 나오면 정적,
    # 안 나오면 렌더다. 값을 주면 그 모드로만 만들고 판정이 그것을 덮어쓰지 않는다.
    # 등록 화면은 이 값을 보내지 않는다 (`app/api/ui_crawlers.py`)
    render_mode: str = ""


class RenderModeUpdate(BaseModel):
    """`static` 과 `playwright` 둘뿐이다. 승격은 운영자가 정한다."""

    render_mode: str


class CollectModesUpdate(BaseModel):
    """목록과 상세를 각각 무엇으로 가져올지. 셋 중 하나씩이고 섞어 쓰는 것이 정상이다."""

    list_mode: str
    detail_mode: str


class NameUpdate(BaseModel):
    """크롤러 이름만 바꾼다. 등록할 때 정한 이름이 URL 이면 사람이 읽지 못한다."""

    name: str


class CompanyUpdate(BaseModel):
    """모회사 이름만 바꾼다. 빈 문자열은 지운다는 뜻이다."""

    default_company: str = ""


class UsageOut(BaseModel):
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    latency_ms: int


class CrawlerOut(BaseModel):
    """등록 결과. `failed_fields` 가 비어야 테스트 실행으로 넘어갈 만하다.

    `skipped_fields` 는 판정하지 않은 필드다. 두 가지가 들어간다 — 상세 URL 없이 등록해서
    돌려볼 HTML 이 없었던 상세 필드와, 모델이 "사이트에 그 항목이 없다"고 답해 셀렉터가 비어
    있는 선택 필드다. 어느 쪽도 매칭은 0개지만 실패가 아니다. 0개 매칭을 성공으로 적으면
    운영자는 못 뽑은 필드와 원래 없는 필드를 구분할 수 없다.
    """

    id: int
    name: str
    status: str
    default_company: str | None
    render_mode: str
    detail_url: str | None
    selectors: SelectorSet
    matches: dict[str, int]
    failed_fields: list[str]
    skipped_fields: list[str]
    notes: list[str]
    usage: UsageOut
    # 등록할 때 스스로 정한 경로와 그 근거. 저장된 값이 `list_mode`/`detail_mode` 이고,
    # `evidence` 는 무엇을 보고 그렇게 정했는지 사람이 읽는 한 줄이다. 판정하지 못했으면
    # `reason` 에 사유가 있고 모드는 운영자가 고른 값 그대로다
    list_mode: str = STATIC
    detail_mode: str = STATIC
    path_evidence: str = ""
    path_reason: str = ""
    path_failure: str = ""


class RenderModeOut(BaseModel):
    """전환 결과. 저장된 값을 그대로 돌려준다."""

    id: int
    render_mode: str


class CollectModesOut(BaseModel):
    """경로 수정 결과. 저장된 값을 그대로 돌려준다."""

    id: int
    list_mode: str
    detail_mode: str


class NameOut(BaseModel):
    """이름 수정 결과. 저장된 값을 그대로 돌려준다."""

    id: int
    name: str


class CompanyOut(BaseModel):
    """회사명 수정 결과. 저장된 값을 그대로 돌려준다."""

    id: int
    default_company: str | None


class DeleteOut(BaseModel):
    """삭제 결과. 함께 지운 테스트 실행 기록 수를 같이 돌려준다."""

    id: int
    name: str
    deleted_test_runs: int


class SelectorsOut(BaseModel):
    """수동 보정 결과. `status` 는 보정으로 바뀌지 않는다."""

    id: int
    status: str
    selectors: SelectorSet


class ApiConfigOut(BaseModel):
    """API 설정 수동 보정 결과. `status` 는 보정으로 바뀌지 않는다."""

    id: int
    status: str
    api_config: ApiConfig


class SelectorChangeOut(BaseModel):
    """필드 하나가 어떻게 바뀌었는지. 화면이 전/후를 나란히 적는 데 쓴다."""

    name: str
    before: str
    after: str


class RepairIn(BaseModel):
    """고치기 요청. 본문 없이 불러도 된다 — 그때는 힌트 없이 지금까지처럼 돈다.

    `hint` 는 자유 입력이다. F12 의 `Copy selector` 가 뱉은 경로일 수도 있고 "마감일은 목록
    두 번째 줄에 있다" 같은 문장일 수도 있다. 어느 쪽이든 그냥 사람이 준 단서로 프롬프트에
    실린다. 상한과 "그대로 베껴 쓰지 말라"는 지시는 `app/selector/repair.py` 가 건다.
    """

    hint: str = ""


class RepairOut(BaseModel):
    """AI 수정 결과. **저장하지 않는다.**

    `saved` 가 늘 거짓인 것은 자리를 채우려는 필드가 아니다. 이 응답을 읽는 쪽이 "고쳤으니
    반영됐겠지"로 넘어가지 않게 하려는 것이다. `crawlers.selectors_json` 을 바꾸는 경로는
    지금까지처럼 `PUT /api/crawlers/{id}/selectors` 하나뿐이고, 운영자가 전/후를 보고 저장을
    누른다 (`.claude/rules/llm.md`).

    `before_matches` 와 `after_matches` 는 **같은 HTML** 에 돌린 판정이다. 그래야 매칭
    개수의 차이가 셀렉터 변화 때문이라고 말할 수 있다.
    """

    id: int
    status: str
    saved: bool
    selectors: SelectorSet
    before_matches: dict[str, int]
    after_matches: dict[str, int]
    # `failed` 면 실패한 필드를 고친 것이고, `hinted` 면 실패는 없는데 운영자가 힌트로 지적한
    # 자리를 고친 것이다. 화면이 뭐라고 적을지가 여기서 갈린다
    mode: str
    targets: list[str]
    # `targets` 중 실제로 실패였던 것. 힌트가 들어오면 대상이 그보다 넓어진다 — 화면이
    # "실패한 필드 N개" 라고 적을 때 세야 하는 것은 이쪽이다
    failed_targets: list[str]
    repaired: list[str]
    unresolved: list[str]
    # 고친 뒤에도 실패로 남은 필드 전부. `unresolved` 는 이번에 고치려 한 것만이라, 대상이
    # 아니었던 실패(상세 HTML 이 없어 판정을 건너뛴 것 말고)가 여기서만 보인다
    failed_fields: list[str]
    skipped_fields: list[str]
    changes: list[SelectorChangeOut]
    notes: list[str]
    usage: UsageOut


class PreviewItem(BaseModel):
    """추출된 값 그대로. 정제 전이라 공백과 줄바꿈이 섞여 있는 것이 정상이다."""

    source_url: str
    state: str
    fields: dict[str, str]


class RunFailure(BaseModel):
    source_url: str
    error_class: str | None
    message: str


class TestRunOut(BaseModel):
    """`crawl_runs` 행에 남은 값과 같은 카운트 + 미리보기.

    `render_mode` 는 이 실행이 실제로 쓴 경로고, `saved_render_mode` 는 크롤러에 저장된
    목록 모드(`crawlers.list_mode`)다. 둘이 다르면 이번 한 번만 다른 모드로 시험한 것이고
    저장값은 그대로다.
    """

    crawler_id: int
    run_id: int
    status: str
    crawler_status: str
    render_mode: str
    saved_render_mode: str
    matched: int
    success_count: int
    new_count: int
    fail_count: int
    # 적재하지 않고 넘긴 수. 마감이 지났거나 이미 아는 공고다. `fail_count` 와 반드시 따로
    # 센다 — 합치면 날짜 형식이 바뀌어 전부 걸러진 사이트가 정상 실행으로 보인다
    # (`migrations/0010_run_failures.sql`)
    skipped_count: int
    error_class: str | None
    error_message: str
    items: list[PreviewItem]
    failures: list[RunFailure]


def get_connection() -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def get_crawl_fetcher() -> FetchPolicy:
    """공용 fetch 클라이언트. 테스트는 이 의존성을 갈아끼운다."""
    return get_fetcher()


def get_generator(
    conn: Annotated[sqlite3.Connection | None, Depends(get_connection)],
) -> GenerateFn:
    """기본 생성 경로. 테스트는 이 의존성을 갈아끼운다.

    연결을 받는 것은 어느 제공자로 만들지가 DB 에 있어서다. 요청마다 다시 읽으므로 화면에서
    바꾼 값이 **다음 호출부터** 적용된다 (`app/llm/settings.py`). 연결 없이 부르면 환경변수
    설정 그대로다.

    `render_mode` 가 비어 있으면 등록이 스스로 정한다. 정적으로 먼저 만들어 보고, 정적
    HTML 에 목록이 아예 없으면 렌더한 HTML 로 한 번 더 만든다. **운영자에게 모드를 묻지
    않는다** — 목록 URL 하나로 등록이 끝나야 하고, 정적으로 되는 사이트에 브라우저 값을
    붙이지도 않는다 (`.claude/rules/crawling.md` 의 "정적이 먼저").

    값을 주면 그 모드로만 만든다. 운영자가 고른 것을 판정이 덮어쓰지 않는다.
    """

    settings = llm_settings.settings_for(conn, SELECTOR_GENERATE)

    async def generate(list_url: str, detail_url: str, render_mode: str) -> GenerationResult:
        if render_mode:
            return replace(
                await _generate(list_url, detail_url, render_mode, settings),
                render_mode=render_mode,
            )

        static_result = await _generate(list_url, detail_url, STATIC, settings)
        static_weak = _weak_list(static_result)
        if not static_result.verification.list_missing and not static_weak:
            return replace(static_result, render_mode=STATIC)

        if static_weak and not static_result.verification.list_missing:
            # 정적 HTML 에서 반복은 잡았지만 항목이 한 건뿐이거나 따라갈 링크가 없다. CJ 실측
            # (2026-09-17): 목록을 JS 로 채우는 페이지의 껍데기에 항목 모양이 한 건 있어 그것으로
            # 셀렉터를 만들었고, 그 셀렉터는 렌더한 페이지에서 0건이었다. 렌더해서 더 나은 결과가
            # 나올 때만 바꾼다 — 링크를 목록에 두지 않는 사이트(삼성)나 공고가 정말 한 건인
            # 목록은 그대로다
            logger.info("정적 목록이 약해 렌더로도 만들어 본다 url=%s", list_url)
        else:
            logger.info("정적 HTML 에 목록이 없어 렌더로 다시 만든다 url=%s", list_url)
        try:
            rendered = await _generate(list_url, detail_url, PLAYWRIGHT, settings)
        except FetchError as exc:
            # 브라우저가 없거나 렌더가 실패했다. 정적 결과를 그대로 올려 무엇이 안 됐는지
            # 운영자가 보게 한다 — 여기서 예외를 던지면 사유가 렌더 실패로 바뀐다
            logger.warning("렌더 생성도 실패했다 url=%s: %s", list_url, exc)
            return replace(
                static_result,
                render_mode=STATIC,
                notes=[*static_result.notes, f"렌더로 다시 만들지도 못했다: {exc}"],
            )

        if static_weak and not static_result.verification.list_missing:
            if not _better_list(rendered, static_result):
                return replace(
                    static_result,
                    render_mode=STATIC,
                    notes=[
                        *static_result.notes,
                        "정적 목록이 약해 렌더로도 만들어 봤지만 나아지지 않았다",
                    ],
                )
            return replace(
                rendered,
                render_mode=PLAYWRIGHT,
                notes=[
                    *rendered.notes,
                    "정적 목록이 약해(항목 한 건 이하이거나 링크 없음) 렌더한 HTML 로 "
                    "셀렉터를 만들었다",
                ],
            )

        if rendered.verification.list_missing:
            return replace(rendered, render_mode=PLAYWRIGHT)
        return replace(
            rendered,
            render_mode=PLAYWRIGHT,
            notes=[
                *rendered.notes,
                "정적 HTML 에는 목록이 없어 렌더한 HTML 로 셀렉터를 만들었다",
            ],
        )

    return generate


def _links_failed(result: GenerationResult) -> bool:
    """목록에서 따라갈 링크를 하나도 읽지 못했는가. 링크가 없다고 답한 경우(건너뜀)는 아니다."""
    return "list.link" in result.verification.failed_list_fields


def _item_count(result: GenerationResult) -> int:
    return int(result.verification.summary().get("list.item", 0))


def _weak_list(result: GenerationResult) -> bool:
    """정적으로 만든 목록이 렌더로도 만들어 볼 만큼 약한가. 항목이 한 건 이하이거나 링크가 없다."""
    return _links_failed(result) or _item_count(result) < 2


def _better_list(rendered: GenerationResult, static: GenerationResult) -> bool:
    """렌더로 만든 목록이 정적보다 나은가.

    항목이 더 많이 잡히면 낫다 — 링크를 못 읽어도 된다. 링크는 등록 판정이 항목을 눌러 찾는다.
    CJ 실측(2026-09-17): 렌더한 목록 10건의 링크가 `javascript:` 라 "링크 실패" 로 보고 빈 목록
    안내 한 건짜리 정적 결과를 남겼다. 항목 수가 같으면 링크를 읽는 쪽이 낫다.
    """
    if rendered.verification.list_missing:
        return False
    if _item_count(rendered) != _item_count(static):
        return _item_count(rendered) > _item_count(static)
    return _links_failed(static) and not _links_failed(rendered)


async def _generate(
    list_url: str, detail_url: str, render_mode: str, settings: Settings | None = None
) -> GenerationResult:
    """한 경로로 한 번 만든다. 렌더 모드면 브라우저가 이 블록 안에서만 산다."""
    async with open_source(render_mode, get_fetcher()) as source:
        if not detail_url.strip():
            # 상세 페이지 주소가 없는 사이트다. 없는 주소를 지어내 가져오지 않는다.
            # 목록만 보고 만들고, 상세 셀렉터는 볼 HTML 이 없어 판정되지 않는다
            list_html = (await source.fetch(list_url)).text
            return await generate_from_html(list_html, "", list_url=list_url, settings=settings)
        return await generate_for_urls(list_url, detail_url, source=source, settings=settings)


def get_repairer(
    conn: Annotated[sqlite3.Connection | None, Depends(get_connection)],
) -> RepairFn:
    """기본 고치기 경로. 테스트는 이 의존성을 갈아끼운다.

    가져오기는 생성과 같은 경로다. HTML 은 어디에도 보관하지 않으므로 고칠 때 다시 가져오고,
    그 요청도 공용 fetch 클라이언트의 딜레이와 robots 아래에서 돈다
    (`.claude/rules/crawling.md`).

    제공자는 생성과 따로 고른다. 연결에서 읽는 이유는 생성 쪽과 같다.
    """
    settings = llm_settings.settings_for(conn, SELECTOR_REPAIR)

    async def repair(
        list_url: str,
        detail_url: str,
        render_mode: str,
        selectors: SelectorSet,
        *,
        hint: str = "",
    ) -> RepairOutcome:
        async with open_source(render_mode, get_fetcher()) as source:
            return await repair_for_urls(
                list_url, detail_url, selectors, source=source, hint=hint, settings=settings
            )

    return repair


def get_discoverer() -> DiscoverFn:
    """기본 경로 판정. 테스트는 이 의존성을 갈아끼운다.

    브라우저는 필요한 순간에만 뜬다. 목록을 정적으로 받아 항목에 상세 주소까지 있으면
    `discover_detail_path()` 가 `open_probe` 를 한 번도 부르지 않고, 그때 Chromium 은
    실행되지 않는다 (`.claude/rules/crawling.md`).
    """

    async def discover(list_url: str, selectors: SelectorSet) -> Discovery:
        fetcher = get_fetcher()
        renderer = Renderer(fetcher)
        try:
            return await discover_detail_path(
                list_url, selectors, fetcher=fetcher, open_probe=renderer.open_probe
            )
        finally:
            # 브라우저를 띄운 적이 없으면 아무 일도 하지 않는다
            await renderer.aclose()

    return discover


async def _discover_path(
    discover: DiscoverFn, list_url: str, selectors: SelectorSet, render_mode: str
) -> Discovery:
    """경로를 알아본다. 알아내지 못해도 등록은 계속된다.

    판정은 제안이다 (`app/selector/discovery.py`). 여기서 예외가 나면 방금 만든 셀렉터까지
    같이 사라지는데, 그것은 운영자가 손으로 고칠 대상마저 없애는 것이다. 못 알아냈다는 사실을
    화면에 적고 모드는 운영자가 고른 값 그대로 둔다.
    """
    try:
        return await discover(list_url, selectors)
    except Exception as exc:
        # 브라우저가 없거나 사이트가 도중에 끊긴 경우다. 사유는 화면에 그대로 나간다
        logger.warning("crawler registration: 경로를 판정하지 못했다: %s", exc)
        return Discovery(
            list_mode=render_mode,
            evidence="",
            reason=f"경로를 판정하는 중에 실패했다: {type(exc).__name__}: {exc}",
        )


@router.post("", response_model=CrawlerOut, status_code=201)
async def create_crawler(
    payload: CrawlerCreate,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    generate: Annotated[GenerateFn, Depends(get_generator)],
    discover: Annotated[DiscoverFn, Depends(get_discoverer)],
) -> CrawlerOut:
    # 비우면 등록이 스스로 정한다. 값을 주면 그 모드로만 만들고 판정이 덮어쓰지 않는다
    requested = _requested_render_mode(payload.render_mode)
    try:
        result = await generate(payload.list_url, payload.detail_url, requested)
    except RobotsDisallowedError as exc:
        # robots 가 막은 URL 은 등록 자체를 거절한다 (`.claude/rules/crawling.md`).
        raise HTTPException(
            status_code=400, detail={"reason": "robots", "message": str(exc)}
        ) from exc
    except FetchError as exc:
        raise HTTPException(
            status_code=502, detail={"reason": exc.error_class, "message": str(exc)}
        ) from exc
    except SelectorGenerationError as exc:
        status = 500 if exc.reason == "no_api_key" else 502
        raise HTTPException(
            status_code=status, detail={"reason": exc.reason, "message": str(exc)}
        ) from exc

    if result.verification.list_missing:
        # 목록이 없다. 셀렉터를 손으로 고쳐도 잡을 노드가 없으므로 행을 남기지 않는다.
        # 렌더까지 해 보고도 없었는지는 `result.render_mode` 가 말해 준다 — 다음 수단이
        # 다르기 때문에 사유 문장에서 갈라 적는다
        failed = ", ".join(result.verification.failed_list_fields)
        tried = (
            "정적으로도 렌더로도 목록을 찾지 못했다. 주소가 목록 페이지가 맞는지, 로그인이나 "
            "검색 조건이 있어야 목록이 나오는 사이트인지 확인한다"
            if result.render_mode == PLAYWRIGHT
            else "목록을 찾지 못했다"
        )
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "list_not_found",
                "message": f"{tried}. 목록 필드 {failed} 가 모두 0개 매칭이다",
                "failed_fields": result.verification.failed,
                "matches": result.verification.summary(),
                "notes": result.notes,
            },
        )

    if result.verification.list_fields_missing:
        # 항목은 잡혔는데 그 안이 비었다. 목록이 없는 것과 사유가 다르다 — 여기서는 목록을
        # 찾았으므로 다음 수단이 렌더 승격이 아니라 항목 셀렉터를 다시 잡는 것이다.
        failed = ", ".join(result.verification.failed_list_fields)
        matched = result.verification.summary().get("list.item", 0)
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "list_fields_not_found",
                "message": (
                    f"목록 항목은 찾았으나 그 안에서 필드를 뽑지 못했다. 항목 {matched}건을 "
                    f"잡고도 {failed} 가 모두 0개 매칭이다. 항목 셀렉터가 공고 목록이 아닌 "
                    "다른 반복 요소를 잡았거나, 항목 안의 셀렉터가 실제 구조와 다르다"
                ),
                "failed_fields": result.verification.failed,
                "matches": result.verification.summary(),
            },
        )

    # 셀렉터가 있어야 목록 항목을 잡을 수 있으므로 생성 다음이다. 정적으로 상세 주소까지
    # 나오면 브라우저는 뜨지 않는다 (`app/selector/discovery.py`)
    discovery = await _discover_path(
        discover, payload.list_url, result.selectors, result.render_mode
    )
    # 판정에 성공했을 때만 그 경로를 저장한다. 실패하면 셀렉터를 만든 경로 그대로 두고,
    # 무엇이 안 됐는지는 화면에 적힌다.
    #
    # 운영자가 렌더를 고른 등록은 렌더로 남는다. 정적으로도 목록이 잡히더라도 판정이 그 선택을
    # 내려앉히지 않는다 — 고른 값을 자동 판정이 덮어쓰지 않는다는 규칙이 등록 순간에도 같다
    # (`.claude/tasks/done/fill-body/prd-fill-body.md` 5절).
    #
    # 목록 API 는 상세 판정이 실패해도 살린다. 그것은 이미 `httpx` 로 다시 불러 확인한
    # 사실이고, 상세로 가는 길을 못 찾았다는 것과 별개다 (`app/selector/list_api.py`).
    # 셀렉터를 만든 경로. 운영자가 고른 값이 있으면 생성이 그것으로 돌았다
    generated_mode = requested or result.render_mode
    discovered_list = (
        discovery.list_mode if (discovery.ok or discovery.list_adopted) else generated_mode
    )
    list_mode = PLAYWRIGHT if requested == PLAYWRIGHT else discovered_list
    # 상세를 실제로 열어 봤으면 그때 알아낸 방식이 맞다. 판정이 실패했어도 공고 한 건은 연
    # 경우가 있다 — 나머지 공고로 갈 길만 없었다 (`app/selector/discovery.py`)
    opened_detail = discovery.ok or discovery.detail is not None
    detail_mode = (discovery.detail_mode or discovered_list) if opened_detail else generated_mode
    api_config_json = _discovered_api_config(discovery)

    # 상세 URL 없이 등록했는데 판정이 상세 문서 주소를 알아냈으면, 그 페이지를 보고 상세
    # 셀렉터를 만든다. 여기까지 와야 목록 URL 하나로 등록이 끝난다 — 상세 셀렉터가 비어 있는
    # 크롤러는 첫 실행에서 본문을 못 읽는다
    sample_detail_url = payload.detail_url.strip() or _discovered_detail_url(discovery)
    if not payload.detail_url.strip() and sample_detail_url:
        result = await _fill_detail(
            generate,
            result,
            list_url=payload.list_url,
            detail_url=sample_detail_url,
            render_mode=discovery.detail_mode or generated_mode,
        )

    # 항목이 `href` 를 안 들고 있어 판정이 상세 주소 형식을 알아냈으면 그것을 셀렉터에 얹는다.
    # 확인된 것만 온다 (`app/selector/link_probe.py`)
    selectors = _with_link(result.selectors, discovery)

    name = payload.name.strip() or urlsplit(payload.list_url).netloc
    # 안 적었으면 NULL 이다. 빈 문자열로 넣으면 "회사명이 있다" 와 구분되지 않는다
    default_company = payload.default_company.strip() or None
    # 판정이 찾아낸 상세 주소도 저장한다. 나중에 AI 수정이 상세 셀렉터를 고치려면 볼 페이지가
    # 있어야 하고, 그 주소는 이미 열어서 확인한 것이다
    detail_url = sample_detail_url or None
    cursor = conn.execute(
        """
        INSERT INTO crawlers
               (name, list_url, detail_url, selectors_json, status, default_company,
                list_mode, detail_mode, api_config_json)
        VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?)
        """,
        (
            name,
            payload.list_url,
            detail_url,
            selectors.to_json(),
            default_company,
            list_mode,
            detail_mode,
            api_config_json,
        ),
    )
    crawler_id = int(cursor.lastrowid or 0)

    matches = result.verification.summary()
    resolved_links = _link_matches(discovery)
    if resolved_links is not None:
        # 생성 시점 판정은 `list.link` 를 실패로 적었다. 그 뒤 판정이 주소 형식을 알아내
        # 항목에서 실제로 주소가 나왔으므로 그 수로 바꿔 적는다
        matches["list.link"] = resolved_links
    unverified = _skipped_detail_fields(matches, detail_url)
    # 모델이 "사이트에 그 항목이 없다"고 답해 셀렉터가 비어 있는 필드. 매칭 0개지만 고칠
    # 셀렉터가 없어 실패가 아니다 (`app/selector/verify.py`)
    absent = [name for name in result.verification.skipped if name not in unverified]
    # 화면의 표 순서대로 적는다. 어느 줄이 건너뛴 것인지 위에서 아래로 짚을 수 있어야 한다
    skipped = [name for name in matches if name in unverified or name in absent]
    notes = list(result.notes)
    if unverified:
        notes.append(
            "상세 URL 이 없어 상세 셀렉터를 확인하지 못했다. 볼 페이지가 없어 판정을 건너뛴 "
            "것이라 실패가 아니다. 실제로 맞는지는 테스트 실행이 말해 준다"
        )
    if absent:
        notes.append(
            f"모델이 사이트에 없다고 답해 셀렉터가 비어 있는 필드가 있다: {', '.join(absent)}. "
            "매칭 0개지만 고칠 셀렉터가 없으므로 실패가 아니라 건너뜀이다"
        )
    if discovery.link is not None:
        notes.append(
            f"항목에 상세 주소가 없어 판정이 형식을 알아내 `list.link` 와 `link_template` 을 "
            f"채웠다: {discovery.link.template}"
        )

    # 생성 1회가 행 1개다. 상세 셀렉터를 위해 두 번 불렀으면 그 둘을 합친 값이 들어간다
    # (`_added`). 남기지 못해도 등록은 그대로 간다 (`app/llm/log.py`)
    record_call(conn, feature=SELECTOR_GENERATE, usage=result.usage)

    return CrawlerOut(
        id=crawler_id,
        name=name,
        status="draft",
        default_company=default_company,
        render_mode=generated_mode,
        detail_url=detail_url,
        selectors=selectors,
        matches=matches,
        # 건너뛴 필드는 실패에서 뺀다. 고칠 곳을 알려 주는 목록에 확인 못 한 것을 섞지 않는다
        failed_fields=[
            name
            for name in result.verification.failed
            if name not in skipped and matches.get(name, 0) == 0
        ],
        skipped_fields=skipped,
        notes=notes,
        usage=UsageOut(**vars(result.usage)),
        list_mode=list_mode,
        detail_mode=detail_mode,
        path_evidence=discovery.evidence,
        path_reason=discovery.reason,
        path_failure=discovery.failure,
    )


def _discovered_detail_url(discovery: Discovery) -> str:
    """판정이 알아낸 상세 문서 주소. 없으면 빈 문자열이다.

    API 로 가져오는 상세는 사람이 볼 페이지가 아니라 셀렉터를 만들 대상이 아니다.

    판정이 실패해도 상세 문서를 실제로 열었으면 그 주소를 쓴다. 공고 한 건은 열었는데 나머지로
    갈 길이 없는 경우다 (`app/selector/discovery.py`). 운영자가 목록 경로를 손으로 채우면 바로
    돌 수 있게, 상세 셀렉터는 그 페이지로 미리 만들어 둔다.
    """
    if discovery.detail is None or discovery.detail.kind != DOCUMENT:
        return ""
    return discovery.detail.url


async def _fill_detail(
    generate: GenerateFn,
    result: GenerationResult,
    *,
    list_url: str,
    detail_url: str,
    render_mode: str,
) -> GenerationResult:
    """알아낸 상세 페이지로 상세 셀렉터만 다시 만든다. 목록 쪽은 이미 만든 것을 쓴다.

    실패해도 등록은 계속된다. 상세 셀렉터가 비어 있는 크롤러는 쓸모가 적지만, 목록까지
    버리면 운영자가 손으로 고칠 대상마저 없어진다 (`.claude/rules/llm.md`).
    """
    try:
        second = await generate(list_url, detail_url, render_mode)
    except (FetchError, SelectorGenerationError) as exc:
        logger.warning("상세 셀렉터를 만들지 못했다 url=%s: %s", detail_url, exc)
        return replace(
            result,
            notes=[
                *result.notes,
                f"알아낸 상세 주소 {detail_url} 로 상세 셀렉터를 만들지 못했다: {exc}",
            ],
        )

    fields = [one for one in result.verification.fields if one.name.startswith("list.")]
    fields += [one for one in second.verification.fields if one.name.startswith("detail.")]
    extra = [note for note in second.notes if note not in result.notes]
    return replace(
        result,
        selectors=result.selectors.model_copy(update={"detail": second.selectors.detail}),
        verification=VerificationReport(fields=fields),
        usage=_added(result.usage, second.usage),
        notes=[
            *result.notes,
            *extra,
            f"상세 URL 을 판정이 찾아 그 페이지로 상세 셀렉터를 만들었다: {detail_url}",
        ],
    )


def _added(first: Usage, second: Usage) -> Usage:
    """두 번 부른 생성의 비용을 합친다. 비용 질문에 답하려면 둘 다 세야 한다."""
    return Usage(
        provider=first.provider,
        model=first.model,
        input_tokens=first.input_tokens + second.input_tokens,
        output_tokens=first.output_tokens + second.output_tokens,
        total_tokens=first.total_tokens + second.total_tokens,
        latency_ms=first.latency_ms + second.latency_ms,
    )


def _with_link(selectors: SelectorSet, discovery: Discovery) -> SelectorSet:
    """판정이 알아낸 상세 주소 형식을 셀렉터에 얹는다. 없으면 그대로 둔다.

    모델은 상세 URL 형식을 이 HTML 만으로 알 수 없어 `link_template` 을 비워 둔다
    (`app/selector/generator.py`). 그 자리를 채우는 것은 클릭으로 알아내고 두 건을 실제로
    열어 확인한 형식뿐이다 — 확인되지 않은 형식은 여기까지 오지 않는다.
    """
    if discovery.link is None:
        return selectors
    return selectors.model_copy(update={"list": discovery.link.selectors(selectors.list)})


def _link_matches(discovery: Discovery) -> int | None:
    """알아낸 주소 형식으로 주소가 나온 항목 수. 형식이 없으면 None 이다."""
    return discovery.link.resolved if discovery.link is not None else None


def _discovered_api_config(discovery: Discovery) -> str | None:
    """알아낸 목록·상세 API 설정. 문서를 그대로 여는 경로면 저장할 설정이 없다.

    설정 없이 모드만 `api` 로 저장하면 등록만 성공하고 이후 실행이 전부 실패한다. 모드와
    설정은 같이 저장되거나 같이 저장되지 않는다.

    목록과 상세는 따로 정해진다. 목록만 API 인 크롤러(카카오·우아한형제들)와 상세만 API 인
    크롤러(삼성)가 둘 다 정상적인 조합이다 (`app/crawler/collect.py`).
    """
    list_config = (
        discovery.list.config() if discovery.list is not None and discovery.list.ok else None
    )
    detail_config = (
        discovery.detail.api.detail
        if discovery.ok and discovery.detail is not None and discovery.detail.api is not None
        else None
    )
    if list_config is None and detail_config is None:
        return None
    return ApiConfig(list=list_config, detail=detail_config).to_json()


def _skipped_detail_fields(matches: dict[str, int], detail_url: str | None) -> list[str]:
    """상세 URL 없이 생성했을 때 판정하지 않은 필드.

    상세 셀렉터는 모델이 낸 그대로 저장되지만 어느 HTML 에도 돌려보지 않았다. 0개 매칭을
    실패로 적으면 운영자는 고칠 곳으로 읽는데, 여기서는 볼 페이지가 없었을 뿐이다.
    """
    if detail_url:
        return []
    return [name for name in matches if name.startswith("detail.")]


def _requested_render_mode(value: str) -> str:
    """운영자가 고른 모드. 비어 있으면 빈 문자열이고, 그때는 등록이 스스로 정한다.

    `_validated_render_mode` 와 갈라 두는 이유는 "안 골랐다" 와 "정적을 골랐다" 가 다른
    뜻이 됐기 때문이다. 앞은 판정에 맡기는 것이고 뒤는 판정이 덮어쓰지 않는 선택이다.
    """
    mode = value.strip()
    return _validated_render_mode(mode) if mode else ""


def _validated_render_mode(value: str) -> str:
    """모르는 값은 거절한다. 조용히 다른 모드로 되돌리면 운영자는 고른 줄 알고 기다린다."""
    mode = value.strip() or DEFAULT_RENDER_MODE
    if mode not in RENDER_MODES:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "unknown_render_mode",
                "message": f"render_mode 는 {', '.join(RENDER_MODES)} 중 하나다: {value}",
            },
        )
    return mode


@router.put("/{crawler_id}/render-mode", response_model=RenderModeOut)
def update_render_mode(
    crawler_id: int,
    payload: RenderModeUpdate,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> RenderModeOut:
    """정적과 렌더 사이를 옮긴다. 자동 승격은 없다 — 이 경로로만 바뀐다.

    바꾼다고 셀렉터가 다시 생성되지는 않는다. 정적으로 만든 셀렉터는 렌더된 DOM 과 다를 수
    있으므로, 올린 뒤에는 테스트 실행으로 다시 확인하는 것이 순서다
    (`.claude/skills/crawl-test/SKILL.md`).
    """
    row = conn.execute("SELECT id FROM crawlers WHERE id = ?", (crawler_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"message": f"크롤러 {crawler_id} 가 없다"})

    mode = _validated_render_mode(payload.render_mode)
    # 목록과 상세를 한 값으로 함께 옮긴다. 이 경로는 정적과 렌더 사이만 오가고, 둘을 갈라
    # 고르는 것은 화면이 붙는 Push 25 다
    conn.execute(
        "UPDATE crawlers SET list_mode = ?, detail_mode = ? WHERE id = ?",
        (mode, mode, crawler_id),
    )
    return RenderModeOut(id=crawler_id, render_mode=mode)


@router.put("/{crawler_id}/collect-modes", response_model=CollectModesOut)
def update_collect_modes(
    crawler_id: int,
    payload: CollectModesUpdate,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> CollectModesOut:
    """목록과 상세의 경로를 각각 정한다. 운영자가 정한 값이고 자동으로 되돌아가지 않는다.

    `PUT /{id}/render-mode` 와 나눠 둔 이유는 담을 수 있는 값이 다르기 때문이다. 그쪽은 정적과
    렌더 사이만 오가고 목록·상세를 같은 값으로 함께 옮긴다 — `api` 로 도는 크롤러에 그것을
    쓰면 알아낸 API 경로가 조용히 정적으로 내려앉는다. 이 경로는 세 값을 다 받고 목록과 상세를
    따로 정한다.

    설정은 건드리지 않는다. `api_config_json` 과 셀렉터는 그대로 있고, 바꾼 경로로 실제로
    가져와지는지는 테스트 실행이 말해 준다.
    """
    row = conn.execute("SELECT id FROM crawlers WHERE id = ?", (crawler_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"message": f"크롤러 {crawler_id} 가 없다"})

    list_mode = _validated_collect_mode(payload.list_mode, "list_mode")
    detail_mode = _validated_collect_mode(payload.detail_mode, "detail_mode")
    conn.execute(
        "UPDATE crawlers SET list_mode = ?, detail_mode = ? WHERE id = ?",
        (list_mode, detail_mode, crawler_id),
    )
    return CollectModesOut(id=crawler_id, list_mode=list_mode, detail_mode=detail_mode)


def _validated_collect_mode(value: str, field: str) -> str:
    """모르는 값은 거절한다. 저장하고 나서 실행이 실패하는 것보다 지금 거절하는 편이 낫다."""
    mode = value.strip()
    if mode not in COLLECT_MODES:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "unknown_collect_mode",
                "message": f"{field} 는 {', '.join(COLLECT_MODES)} 중 하나다: {value}",
            },
        )
    return mode


@router.put("/{crawler_id}/name", response_model=NameOut)
def update_name(
    crawler_id: int,
    payload: NameUpdate,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> NameOut:
    """크롤러 이름을 고친다.

    등록이 이름을 안 받으면 리스트 URL 의 호스트를 그대로 쓴다(`create_crawler`). 그래서
    `career.doosan.com` 같은 행이 남고, 화면과 워크플로우 이름이 전부 그 값을 읽는다.
    다시 등록하면 판정이 다시 돌아 브라우저와 모델을 쓰므로, 이름만 바꾸는 길을 둔다.

    빈 이름은 거절한다. `crawlers.name` 은 NOT NULL 이고, 목록에서 그 행을 알아보는 유일한
    값이다 — 지울 수 있는 `default_company` 와 다르다.

    이미 만들어진 워크플로우의 이름은 따라오지 않는다. 워크플로우는 만들 때 이름을 복사해
    자기 행에 들고 있고(`app/api/workflows.py`), 그 값을 여기서 덮으면 운영자가 워크플로우에
    따로 붙여 둔 이름이 사라진다.
    """
    row = conn.execute("SELECT id FROM crawlers WHERE id = ?", (crawler_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"message": f"크롤러 {crawler_id} 가 없다"})

    name = payload.name.strip()
    if not name:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": "empty_name",
                "message": "이름은 비울 수 없다. 목록에서 이 크롤러를 알아볼 값이 없어진다",
            },
        )
    conn.execute("UPDATE crawlers SET name = ? WHERE id = ?", (name, crawler_id))
    return NameOut(id=crawler_id, name=name)


@router.put("/{crawler_id}/company", response_model=CompanyOut)
def update_company(
    crawler_id: int,
    payload: CompanyUpdate,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> CompanyOut:
    """모회사 이름을 고친다. 빈 문자열은 지운다는 뜻이다.

    등록 화면(`POST /ui/crawlers`)은 이 칸을 필수로 받지만, 이 API 자체는 막지 않는다 —
    비우는 것을 막으면 이미 등록된 크롤러의 모회사를 지웠다가 다시 정하는 정상적인 편집
    흐름까지 막는다 (`app/api/ui_crawlers.py` 가 등록 시점에 이미 걸러낸다).

    이 값은 `normalized_jobs` 에 즉시 반영되지 않는다. 고친 뒤 재정규화를 돌려야 새 값이
    반영된 행이 나온다 (`.claude/tasks/done/job-crawler/tasks-job-crawler-push7.md`).
    """
    row = conn.execute("SELECT id FROM crawlers WHERE id = ?", (crawler_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"message": f"크롤러 {crawler_id} 가 없다"})

    default_company = payload.default_company.strip() or None
    conn.execute(
        "UPDATE crawlers SET default_company = ? WHERE id = ?", (default_company, crawler_id)
    )
    return CompanyOut(id=crawler_id, default_company=default_company)


@router.delete("/{crawler_id}", response_model=DeleteOut)
def delete_crawler(
    crawler_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> DeleteOut:
    """크롤러 정의를 지운다. 되돌릴 수 없다.

    워크플로우가 매달려 있으면 거절한다. 크롤러만 사라지면 그 워크플로우의 `crawl_runs` 와
    `raw_jobs` 가 어느 사이트에서 온 것인지 설명할 수 없게 된다. 지우려면 워크플로우를 먼저
    지워야 한다.

    함께 지우는 것은 승격 전 테스트 실행 기록뿐이다. 그 행은 이 크롤러 하나만 가리키고 있어
    정의가 없으면 읽을 수 없다. 수집한 공고(`raw_jobs`, `normalized_jobs`)는 워크플로우에
    매달려 있고 크롤러 정의와 수명이 다르므로 건드리지 않는다 (`.claude/rules/data-safety.md`).
    """
    row = conn.execute(
        "SELECT id, name, status FROM crawlers WHERE id = ?", (crawler_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"message": f"크롤러 {crawler_id} 가 없다"})

    workflow_ids = [
        int(item["id"])
        for item in conn.execute(
            "SELECT id FROM workflows WHERE crawler_id = ? ORDER BY id", (crawler_id,)
        ).fetchall()
    ]
    if workflow_ids:
        joined = ", ".join(str(item) for item in workflow_ids)
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "has_workflow",
                "message": (
                    f"워크플로우 {joined} 가 이 크롤러를 쓰고 있어 지울 수 없다. "
                    "워크플로우를 먼저 지운 뒤 다시 시도한다"
                ),
            },
        )
    if row["status"] == "promoted":
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "promoted",
                "message": (
                    "승격된 크롤러는 지울 수 없다. 매달린 워크플로우를 먼저 정리한 뒤 "
                    "상태를 되돌려야 한다"
                ),
            },
        )

    deleted = conn.execute(
        "DELETE FROM crawl_runs WHERE crawler_id = ? AND workflow_id IS NULL", (crawler_id,)
    ).rowcount
    conn.execute("DELETE FROM crawlers WHERE id = ?", (crawler_id,))
    return DeleteOut(id=crawler_id, name=str(row["name"]), deleted_test_runs=max(deleted, 0))


@router.put("/{crawler_id}/selectors", response_model=SelectorsOut)
def update_selectors(
    crawler_id: int,
    payload: dict[str, Any],
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> SelectorsOut:
    """운영자가 손으로 고친 셀렉터를 그대로 저장한다. 다시 생성하지 않는다."""
    row = conn.execute("SELECT status FROM crawlers WHERE id = ?", (crawler_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"message": f"크롤러 {crawler_id} 가 없다"})

    try:
        selectors = validate_selectors(payload)
    except SelectorSchemaError as exc:
        raise HTTPException(
            status_code=422, detail={"reason": exc.reason, "message": str(exc)}
        ) from exc

    conn.execute(
        "UPDATE crawlers SET selectors_json = ? WHERE id = ?",
        (selectors.to_json(), crawler_id),
    )
    return SelectorsOut(id=crawler_id, status=row["status"], selectors=selectors)


@router.put("/{crawler_id}/api-config", response_model=ApiConfigOut)
def update_api_config(
    crawler_id: int,
    payload: dict[str, Any],
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
) -> ApiConfigOut:
    """운영자가 손으로 고친 API 설정을 그대로 저장한다. `update_selectors` 의 API 판이다.

    지금까지 `api_config_json` 을 쓰는 경로는 등록 한 곳뿐이었다. 그래서 매핑을 고치려면
    다시 등록하거나 DB 를 직접 고치는 수밖에 없었고, 둘 다 나쁘다 — 다시 등록하면 경로
    판정이 다시 돌아 브라우저와 모델을 쓰고, 직접 고치면 스키마 검증을 지나지 않는다.

    저장하기 전에 `validate_api_config` 를 지난다. 읽는 쪽(`app/crawler/api_source.py`)은
    설정이 이미 검증됐다고 믿고 있어서, 검증을 건너뛴 값이 들어가면 실행 중에야 깨진다.
    """
    row = conn.execute("SELECT status FROM crawlers WHERE id = ?", (crawler_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"message": f"크롤러 {crawler_id} 가 없다"})

    try:
        config = validate_api_config(payload)
    except ApiConfigError as exc:
        raise HTTPException(
            status_code=422, detail={"reason": exc.reason, "message": str(exc)}
        ) from exc

    conn.execute(
        "UPDATE crawlers SET api_config_json = ? WHERE id = ?", (config.to_json(), crawler_id)
    )
    return ApiConfigOut(id=crawler_id, status=str(row["status"]), api_config=config)


@router.post("/{crawler_id}/repair", response_model=RepairOut)
async def repair_selectors(
    crawler_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    repair: Annotated[RepairFn, Depends(get_repairer)],
    payload: RepairIn | None = None,
) -> RepairOut:
    """실패한 필드만 모델에게 다시 고르게 한다. **저장하지 않는다.**

    저장된 셀렉터를 기준으로, 저장된 URL 을 지금 다시 가져와 판정하고, 그 자리에서 실패한
    필드만 고친다. 고친 셀렉터도 같은 HTML 에 다시 돌려 필드별 매칭 개수를 낸다 — 전과 후가
    같은 HTML 에서 나온 숫자여야 차이가 셀렉터 때문이라고 말할 수 있다.

    결과는 응답으로만 나간다. `crawlers.selectors_json` 은 이 호출로 바뀌지 않는다. 운영자가
    전/후를 보고 "셀렉터 저장" 을 누르는 것이 `.claude/rules/llm.md` 가 말하는 그 요청이고,
    누르기 전까지 DB 는 그대로다.

    고친 뒤에도 실패가 남으면 `unresolved` 에 그대로 적는다. 억지로 성공으로 만들지 않는다.

    `payload.hint` 는 운영자가 브라우저에서 보고 준 단서다. 있으면 프롬프트에 함께 실리고,
    없으면 지금까지와 같다. 힌트가 있어도 검증은 그대로다 — 고친 셀렉터는 같은 HTML 에 다시
    돌린다.
    """
    row = conn.execute(
        "SELECT list_url, detail_url, selectors_json, status, list_mode FROM crawlers WHERE id = ?",
        (crawler_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"message": f"크롤러 {crawler_id} 가 없다"})
    if not row["selectors_json"]:
        raise HTTPException(
            status_code=409,
            detail={"reason": "no_selectors", "message": "셀렉터가 없는 크롤러는 고칠 수 없다"},
        )

    try:
        selectors = validate_selectors(json.loads(row["selectors_json"]))
    except (json.JSONDecodeError, SelectorSchemaError) as exc:
        # 저장된 셀렉터가 스키마에 맞지 않는다. 추측해서 고치지 않고 그대로 알린다
        raise HTTPException(
            status_code=409, detail={"reason": "invalid_selectors", "message": str(exc)}
        ) from exc

    detail_url = str(row["detail_url"] or "")
    try:
        outcome = await repair(
            str(row["list_url"]),
            detail_url,
            str(row["list_mode"]),
            selectors,
            hint=payload.hint if payload else "",
        )
    except RobotsDisallowedError as exc:
        raise HTTPException(
            status_code=400, detail={"reason": "robots", "message": str(exc)}
        ) from exc
    except FetchError as exc:
        raise HTTPException(
            status_code=502, detail={"reason": exc.error_class, "message": str(exc)}
        ) from exc
    except SelectorRepairError as exc:
        # 고칠 것이 없는 것은 서버 실패가 아니라 상태다. 나머지는 생성과 같은 사유로 갈린다
        status = 409 if exc.reason == "nothing_to_repair" else 502
        raise HTTPException(
            status_code=status, detail={"reason": exc.reason, "message": str(exc)}
        ) from exc
    except SelectorGenerationError as exc:
        # 호출 경로를 생성과 공유하므로 호출 실패도 같은 예외로 온다. Gemini 한도 초과(429)가
        # 여기로 오고, 그 코드와 메시지가 그대로 화면까지 간다
        status = 500 if exc.reason == "no_api_key" else 502
        raise HTTPException(
            status_code=status, detail={"reason": exc.reason, "message": str(exc)}
        ) from exc

    record_call(conn, feature=SELECTOR_REPAIR, usage=outcome.usage)

    after_matches = outcome.after.summary()
    unverified = _skipped_detail_fields(after_matches, detail_url or None)
    absent = [name for name in outcome.after.skipped if name not in unverified]
    skipped = [name for name in after_matches if name in unverified or name in absent]

    return RepairOut(
        id=crawler_id,
        status=str(row["status"]),
        # 이 호출은 저장하지 않는다. 읽는 쪽이 반영됐다고 넘겨짚지 않게 응답에 적어 둔다
        saved=False,
        selectors=outcome.selectors,
        before_matches=outcome.before.summary(),
        after_matches=after_matches,
        mode="hinted" if outcome.hinted_only else "failed",
        targets=outcome.targets,
        failed_targets=outcome.failed_targets,
        repaired=outcome.repaired,
        unresolved=outcome.unresolved,
        failed_fields=[name for name in outcome.after.failed if name not in skipped],
        skipped_fields=skipped,
        changes=[SelectorChangeOut(**vars(change)) for change in outcome.changes],
        notes=outcome.notes,
        usage=UsageOut(**vars(outcome.usage)),
    )


@router.post("/{crawler_id}/test-run", response_model=TestRunOut)
async def test_run(
    crawler_id: int,
    conn: Annotated[sqlite3.Connection, Depends(get_connection)],
    fetcher: Annotated[FetchPolicy, Depends(get_crawl_fetcher)],
    limit: Annotated[int, Query(ge=1, le=20)] = 3,
    render_mode: Annotated[str, Query()] = "",
) -> TestRunOut:
    """저장된 셀렉터로 실제 페이지를 1회 크롤링한다.

    `limit` 은 상세를 몇 건까지 따라갈지다. 테스트는 3건이면 충분하고, 전체를 도는 것은
    테스트가 아니라 그냥 크롤링이다 (`.claude/skills/crawl-test/SKILL.md`).

    `render_mode` 는 이번 한 번만 다른 경로로 시험하는 값이다. 비우면 저장된 모드로 돈다.
    값을 줘도 저장된 모드(`crawlers.list_mode` 와 `detail_mode`)는 바뀌지 않는다 — 정적으로
    되는지 렌더가 필요한지 비교하는 것이 이 실행의 일이고, 시험할 때마다 저장값이 따라
    바뀌면 비교가 안 된다.
    저장값을 바꾸는 것은 `PUT /api/crawlers/{id}/render-mode` 하나뿐이다.

    워크플로우가 없는 실행이라 `raw_jobs` 에는 적재하지 않는다. 남는 것은 `crawl_runs` 행과
    이 응답의 미리보기뿐이다.
    """
    row = conn.execute(
        "SELECT list_url, selectors_json, status, list_mode, detail_mode, api_config_json "
        "FROM crawlers WHERE id = ?",
        (crawler_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"message": f"크롤러 {crawler_id} 가 없다"})

    saved_mode, saved_detail_mode = str(row["list_mode"]), str(row["detail_mode"])
    # 값을 줬을 때만 이번 실행의 경로가 갈린다. 목록과 상세를 함께 옮긴다 — 이 자리는 정적과
    # 렌더를 비교하는 곳이고, 둘을 갈라 시험하는 화면은 Push 25 다. 저장값은 그대로다
    override = _validated_render_mode(render_mode) if render_mode.strip() else ""
    used_mode = override or saved_mode
    used_detail_mode = override or saved_detail_mode

    if not row["selectors_json"] and not (used_mode == API and used_detail_mode == API):
        raise HTTPException(
            status_code=409,
            detail={"reason": "no_selectors", "message": "셀렉터가 없는 크롤러는 실행할 수 없다"},
        )

    try:
        selectors = collect_selectors(row["selectors_json"], used_mode, used_detail_mode)
    except (json.JSONDecodeError, SelectorSchemaError) as exc:
        # 저장된 셀렉터가 스키마에 맞지 않는다. 추측해서 고치지 않고 그대로 알린다.
        raise HTTPException(
            status_code=409, detail={"reason": "invalid_selectors", "message": str(exc)}
        ) from exc
    try:
        api_config = parse_api_config(row["api_config_json"])
    except ApiConfigError as exc:
        raise HTTPException(
            status_code=409, detail={"reason": "invalid_api_config", "message": str(exc)}
        ) from exc

    async with open_collectors(
        list_mode=used_mode,
        detail_mode=used_detail_mode,
        list_url=row["list_url"],
        selectors=selectors,
        fetcher=fetcher,
        api_config=api_config,
        # 시험 실행도 실제 실행과 같은 원문을 보여 준다. 이미지를 읽으면 AI 호출이 생긴다
        image_reader=LlmImageReader(conn),
    ) as collectors:
        result = await run_once(
            conn,
            RunTarget(
                list_url=row["list_url"],
                selectors=selectors,
                trigger=TEST,
                crawler_id=crawler_id,
            ),
            collectors=collectors,
            limit=limit,
        )

    crawler_status = row["status"]
    # 다른 모드로 한 번 시험한 실행은 상태를 올리지 않는다. 저장된 모드로 돌 때 어떻게 되는지를
    # 말해 주지 않기 때문이다 — 그것으로 tested 를 주면 승격된 워크플로우가 첫 주기에 실패한다
    if result.status == SUCCESS and crawler_status == "draft" and not override:
        conn.execute("UPDATE crawlers SET status = 'tested' WHERE id = ?", (crawler_id,))
        crawler_status = "tested"

    return TestRunOut(
        crawler_id=crawler_id,
        run_id=result.run_id,
        status=result.status,
        crawler_status=crawler_status,
        render_mode=used_mode,
        saved_render_mode=saved_mode,
        matched=result.matched,
        success_count=result.success_count,
        new_count=result.new_count,
        fail_count=result.fail_count,
        skipped_count=result.skipped_count,
        error_class=result.error_class,
        error_message=result.error_message,
        items=[
            PreviewItem(source_url=item.source_url, state=item.state, fields=item.fields)
            for item in result.items
        ],
        failures=[
            RunFailure(
                source_url=failure.source_url,
                error_class=failure.error_class,
                message=failure.message,
            )
            for failure in result.failures
        ],
    )
