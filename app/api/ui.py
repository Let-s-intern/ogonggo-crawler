"""운영 화면의 베이스. 레이아웃·네비게이션과 페이지 라우트가 여기 있다.

서버가 HTML 을 렌더하고 HTMX 가 조각만 갈아 끼운다. 빌드 단계도, 번들러도, 클라이언트 상태
저장소도 없다 (`.claude/docs/tech-stack.md`).

스타일은 Tailwind 를 CDN 에서 받아 클래스로만 준다 (2026-08-22 결정, Push 9 이 Push 6 의
"CSS 없음" 결정을 대체한다). 빌드 단계를 만들지 않는 제약이 그대로라 CLI·PostCSS 파이프라인은
두지 않는다 (`.claude/rules/core.md`). 서빙할 자산이 없으니 정적 파일 마운트도 없다 —
HTMX 와 Tailwind 둘 다 CDN 에서 받는다.

페이지 라우트는 페이지를, 조각 라우트는 조각만 돌려준다. 한 라우트가 헤더를 보고 둘 중 무엇을
원했는지 추측하게 만들지 않는다 (`.claude/agents/ui-worker.md`).

화면은 이미 있는 API 라우터를 그대로 부른다. 같은 동작을 하는 두 번째 경로를 만들지 않는다 —
그렇게 갈라지면 화면에서 되는 일이 API 에서 안 되는 상태가 생긴다.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.crawler.collect import API
from app.crawler.playwright import PLAYWRIGHT, STATIC
from app.selector.api_schema import ApiConfig, ApiConfigError, parse_api_config
from app.selector.schema import SelectorSchemaError, SelectorSet, validate_selectors
from app.storage import s3

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# 화면이 늘 때마다 위 줄이 한 칸씩 길어지면, 늘어난 자리를 찾는 일이 화면 하나 늘리는
# 일보다 커진다. 그래서 위 네비게이션은 묶음 이름만 놓고, 묶음 안의 실제 화면은 그 아래
# 두 번째 줄(`group_nav`)에서 고른다 — `SETTINGS_NAV` 가 이미 하는 일을 세 묶음에 더 쓴다.
#
# 묶음을 가르는 기준은 화면 개수가 아니라 파이프라인 단계다 (2026-08-29 결정,
# `.claude/docs/architecture.md` 의 실행 흐름).
#
# 처음에는 "수집" 하나에 부가 워크플로우까지 넣었다. 부가 워크플로우(LLM 분류·전달)는
# 사이트를 가져오는 일이 아니라 이미 가져온 데이터를 가공하는 일이라, 그 자리는 틀렸다 —
# raw_jobs 를 만드는 것이 수집이고 그것을 읽어 normalized_jobs 를 채우거나 고치는 것이
# 정규화다. 부가 워크플로우는 후자다.
#
# "수집" 묶음의 대표 주소는 `/` 가 아니라 `/crawlers` 다 — 루트는 대시보드가 가져갔다
# (2026-08-29 결정, `app/api/ui_dashboard.py`). 대시보드는 화면 하나뿐이라 두 번째 줄
# 메뉴가 필요 없어서 `NAV_GROUPS` 에 묶지 않고 `NAV` 에 직접 얹는다 — `/settings` 와 같다.
NAV_GROUPS: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "/crawlers",
        "수집",
        (
            ("/crawlers", "크롤러 등록"),
            ("/tests", "테스트 실행"),
            ("/workflows", "워크플로우"),
        ),
    ),
    (
        "/rules",
        "정규화",
        (
            ("/rules", "정규화 규칙"),
            ("/side", "부가 워크플로우"),
            ("/taxonomy", "직무 분류"),
            ("/prompt-rules", "AI 규칙"),
        ),
    ),
    (
        "/review",
        "데이터 확인",
        (
            ("/review", "데이터 확인"),
            ("/complete", "완성 공고"),
            ("/companies", "회사 로고"),
        ),
    ),
)

# 네비게이션. 경로와 이름은 여기 한 곳에서만 정한다. 묶음의 이름과 대표 주소는
# `NAV_GROUPS` 에서 그대로 가져온다 — 두 곳에 따로 적으면 화면 하나가 늘 때 한쪽만 넓어진다
#
# 대시보드(`/`)는 `NAV_GROUPS` 어디에도 없다. `/settings` 와 같은 이유다 — 묶여야 할
# 하위 화면이 없는 화면 하나는 두 번째 줄 메뉴를 만들 이유가 없다
NAV: tuple[tuple[str, str], ...] = (
    ("/", "대시보드"),
    *((path, label) for path, label, _ in NAV_GROUPS),
    ("/settings", "운영 설정"),
)


def _group_of(path: str) -> tuple[str, str, tuple[tuple[str, str], ...]] | None:
    """이 주소가 속한 묶음. 묶음에 없는 주소(`/settings` 등)는 `None` 이다."""
    for group in NAV_GROUPS:
        if any(member_path == path for member_path, _ in group[2]):
            return group
    return None


router = APIRouter(tags=["ui"], include_in_schema=False)


def render(request: Request, name: str, /, **context: Any) -> HTMLResponse:
    """조각 하나를 렌더한다. 조각은 레이아웃을 상속하지 않는다."""
    return templates.TemplateResponse(request, name, context)


def render_page(request: Request, name: str, /, **context: Any) -> HTMLResponse:
    """페이지 하나를 렌더한다. 네비게이션과 현재 위치는 여기서 채운다.

    지금 주소가 `NAV_GROUPS` 의 한 묶음에 속하면, 위 네비게이션은 그 묶음의 대표 주소로
    켜지고(`active`) 그 아래 두 번째 줄에 그 묶음의 화면들이 나온다(`group_nav`,
    `group_active`). 묶이지 않은 주소(`/settings`)는 지금까지와 같다 — 위 네비게이션이 그
    주소로 바로 켜지고 두 번째 줄은 없다(운영 설정은 `render_settings` 가 자기 하위 메뉴를
    이미 그린다).
    """
    path = request.url.path
    group = _group_of(path)
    context.setdefault("nav", NAV)
    context.setdefault("active", group[0] if group else path)
    context.setdefault("group_nav", group[2] if group else None)
    context.setdefault("group_active", path)
    return templates.TemplateResponse(request, name, context)


# 실패 사유별 다음 행동. "500 Internal Server Error" 는 운영자가 할 수 있는 것을 말해 주지
# 않는다. 사유를 아는 만큼은 여기서 다음 수를 적는다
NEXT_STEPS: dict[str, str] = {
    "robots": "robots.txt 가 막은 경로다. 다른 URL 을 쓰거나 사이트에 문의한다",
    "transport": "사이트에 닿지 못했다. URL 과 사이트 상태를 확인하고 다시 시도한다",
    "selector_miss": "가져오기는 됐는데 셀렉터가 아무것도 잡지 못했다. 셀렉터를 손으로 고친다",
    "parse": "잡기는 했는데 값을 읽지 못했다. 그 필드의 셀렉터만 고친다",
    # 항목을 못 잡은 경우와, 항목은 잡혔는데 필수 필드를 못 읽은 경우가 둘 다 여기로 온다.
    # "목록 셀렉터를 고친다" 라고만 적으면 뒤쪽에서 운영자가 멀쩡한 list.item 을 뒤진다
    "list_empty": "쓸 수 있는 항목이 하나도 나오지 않았다. 위 사유가 가리키는 셀렉터를 "
    "고친다 — 항목을 못 잡았으면 list.item, 항목은 잡혔는데 링크를 못 읽었으면 list.link 다",
    "detail_unreachable": "상세에 가지 못했다. 크롤러를 다시 등록해 상세로 가는 길을 찾는다",
    "detail_empty": "상세는 열렸는데 본문이 비었다. 상세의 본문 셀렉터만 고친다",
    "list_not_found": "정적 HTML 에 목록이 없다. 렌더(Playwright) 방식으로 올려 다시 생성한다",
    "no_api_key": "그 기능이 고른 제공자의 API 키가 비어 있다. 환경변수를 채우면 다시 된다",
    "api_error": "생성 모델 호출이 실패했다. 잠시 뒤 다시 생성한다",
    "unknown_provider": "설정의 제공자 이름이 없는 이름이다. gemini/claude/gpt/qwen/ollama 나 "
    "AI 설정 화면에서 추가한 회사 이름이어야 한다",
    "no_schema_support": "그 제공자의 그 모델은 응답을 정해진 목록으로 묶지 못한다. 모델을 바꾼다",
    "no_image_support": "그 제공자는 이미지를 받지 않는다. 이미지를 받는 제공자를 고른다",
    "invalid_provider": "AI 설정 화면에서 추가한 회사의 정의를 읽을 수 없다. 화면에서 정의를 "
    "다시 저장한다",
    "connection_failed": "주소·키·모델·답 형식 강제 방식을 확인하고 다시 테스트한다",
    "unparsable": "모델 응답이 JSON 이 아니었다. 셀렉터를 손으로 쓴다",
    "missing_field": "모델 응답에 필요한 필드가 없다. 셀렉터를 손으로 쓴다",
    "unknown_field": "모델이 스키마에 없는 필드를 냈다. 셀렉터를 손으로 쓴다",
    "not_found": "그 행이 없다. 목록을 다시 불러 확인한다",
    "invalid_input": "보낸 값이 형식에 맞지 않는다. 표시된 항목을 고쳐 다시 보낸다",
    "server_error": "서버가 처리하지 못했다. 서버 로그에 자세한 내용이 남는다",
    # AI 규칙 화면 (`app/classify/prompt_rules.py`)
    "unchanged": "지금 판과 같은 규칙이다. 고친 것이 없으면 저장할 필요가 없다",
    "too_long": "규칙이 길다. 줄이거나 예시로 옮긴다 — 규칙은 공고마다 프롬프트에 실린다",
    "too_many": "예시가 많다. 비슷한 예시를 하나로 합친다",
    "half_example": "예시 줄은 원문과 넣을 값을 둘 다 적거나 둘 다 비운다",
    "unknown_rule_field": "화면에 없는 칸 이름이 왔다. 화면을 새로 불러 다시 저장한다",
    "broken_version": "가장 최근 판을 읽지 못한다. 화면에서 규칙을 고쳐 새 판으로 저장하면 "
    "그 판을 대신한다",
    "no_posting": "시험할 공고를 고른다. 목록이 비었으면 분류를 한 번 돌린다",
    "empty_body": "그 공고에 원문도 본문도 없다. 다른 공고를 고른다",
}


# 실패 사유를 사람이 읽는 낱말로. 저장값은 그대로 영어다 (`.claude/rules/writing.md`).
# 사유를 모르는 실패는 `분류 없음` 이다 — 모르는 실패를 아는 실패로 위장하면 그 사이트를
# 계속 잘못 고치게 된다 (`migrations/0010_run_failures.sql`)
REASON_WORDS: dict[str, str] = {
    "transport": "사이트에 못 닿음",
    "selector_miss": "셀렉터가 빗나감",
    "parse": "값을 못 읽음",
    "list_empty": "목록이 비었음",
    "detail_unreachable": "상세에 못 감",
    "detail_empty": "본문이 비었음",
}
UNKNOWN_REASON_WORD = "분류 없음"


def reason_word(reason: str) -> str:
    """실패 사유 하나의 낱말. 모르는 값이면 `분류 없음` 이다."""
    return REASON_WORDS.get(reason, UNKNOWN_REASON_WORD)


@lru_cache(maxsize=8)
def _zone(name: str) -> tzinfo:
    """이름으로 시간대를 찾는다. 못 찾으면 UTC 다 — 화면이 죽는 것보다 낫다."""
    if not name:
        return UTC
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError):
        # 설정이 틀렸다. 화면은 계속 뜨고, 값 옆의 `UTC` 가 그 사실을 말한다
        logger.warning("DISPLAY_TIMEZONE 을 찾지 못했다: %r. UTC 로 그린다", name)
        return UTC


def display_zone() -> tzinfo:
    """화면에 시각을 그릴 때 쓰는 시간대. 설정에서 오고 기본값은 `Asia/Seoul` 이다."""
    return _zone(get_settings().display_timezone)


def format_time(value: Any) -> str:
    """저장된 UTC 시각을 운영자가 사는 시간대의 문자열로 바꾼다.

    DB 는 `datetime('now')` 로 UTC 를 초까지 넣고(시간대 표시가 없다), 재정규화 진행과
    스케줄러는 시간대가 붙은 ISO 문자열을 넣는다. 둘 다 받아 한 형식으로 낸다.

    저장된 값은 UTC 그대로 둔다. 바꾸는 것은 화면에 그리는 이 순간뿐이다 —
    `normalized_at` 은 제공 API 의 폴링 커서라 값이 밀리면 소비 측의 커서가 어긋난다
    (`.claude/docs/api-contract.md`).

    시간대 약칭(`KST`)을 값에 붙인다. 어느 시간대인지 적혀 있지 않으면 UTC 였던 시절의
    9시간 차이를 볼 때마다 다시 의심하게 된다.

    값이 없으면 빈 문자열이다. 읽지 못한 값은 예외 없이 원문 그대로 돌려준다 — 화면 하나가
    통째로 죽는 것보다 낫다.
    """
    if not value:
        return ""
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text
    if parsed.tzinfo is None:
        # 저장 형식(`YYYY-MM-DD HH:MM:SS`)에는 시간대가 없다. SQLite 가 UTC 로 찍은 값이다
        parsed = parsed.replace(tzinfo=UTC)
    shown = parsed.astimezone(display_zone())
    return f"{shown.strftime('%Y-%m-%d %H:%M:%S')} {shown.strftime('%Z')}".strip()


def collapse_blank_lines(value: Any) -> str:
    """연속된 빈 줄을 하나로 접는다. 화면에 그리는 이 순간만 접고 저장값은 그대로 둔다.

    분류가 낸 값(주요 업무·자격요건 등)에는 규칙(`app/normalize/rules.py`)을 태우지
    않는다 — "있는 글자를 그대로 옮긴다" 는 분류의 원칙이라, 원문의 빈 줄이 여러 줄이면
    그대로 옮겨 적힌다. 그 값 자체를 고치면 원문과 달라지므로, 여기서는 **읽기 전용
    미리보기 화면에서 보여줄 때만** 접는다(`app/templates/fragments/complete_preview.html`).
    """
    if not value:
        return ""
    lines = [line.rstrip() for line in str(value).splitlines()]
    collapsed: list[str] = []
    for line in lines:
        if line == "" and collapsed and collapsed[-1] == "":
            continue
        collapsed.append(line)
    return "\n".join(collapsed).strip()


def next_step(reason: str) -> str:
    """그 실패에서 운영자가 할 수 있는 다음 행동. 모르는 사유면 빈 문자열이다."""
    return NEXT_STEPS.get(reason, "")


# 사람이 읽는 자리에는 단어로 적는다. 저장값은 그대로 영어다 (`.claude/rules/writing.md`).
# 크롤러 등록 화면과 테스트 실행 화면이 같은 값을 다른 말로 적던 자리라, 매핑을 여기 하나만
# 두고 모든 템플릿이 쓰게 한다
MODE_WORDS: dict[str, str] = {STATIC: "정적", PLAYWRIGHT: "렌더"}


def mode_word(mode: str) -> str:
    """모드 이름 하나. 모르는 값이면 저장된 값을 그대로 보여준다."""
    return MODE_WORDS.get(mode, mode)


# 크롤러 하나가 어떤 방식으로 도는지를 적는 낱말. 저장값(`static`/`api`/`playwright`)은 그대로
# 두고 사람이 읽는 자리에만 이 말을 쓴다 (`.claude/tasks/done/fill-body/prd-fill-body.md` 5절).
LIST_WORDS: dict[str, str] = {API: "목록 API", PLAYWRIGHT: "목록 렌더", STATIC: "정적 목록"}
DETAIL_API_WORD = "상세 API"
# 항목의 `a[href]` 를 그대로 따라간다
DETAIL_LINK_WORD = "링크"
# 항목의 값으로 주소를 조립한다 (`link_template` 의 `{id}` 나 `{속성이름}`)
DETAIL_TEMPLATE_WORD = "항목 속성"
# 목록 항목에 상세로 갈 값이 없다. 실패가 아니라 그런 크롤러라는 사실이다
DETAIL_NONE_WORD = "상세 없음"
UNKNOWN_PATH_WORD = "알 수 없음"


@dataclass(frozen=True)
class PathView:
    """크롤러 하나가 목록과 상세를 얻는 법. 판정은 여기 오기 전에 끝나 있다.

    `*_note` 는 낱말 옆에 적는 실제 값이다 — 엔드포인트, 주소 형식, 셀렉터. 낱말만 적으면
    두 크롤러가 같은 낱말을 달고도 서로 다른 곳을 부르는 것을 화면에서 구분할 수 없다.

    `checked_at` 은 저장된 UTC 문자열 그대로다. 시간대 변환은 템플릿의 `as_time` 이 한다.
    """

    list_mode: str
    list_word: str
    list_note: str
    detail_mode: str
    detail_word: str
    detail_note: str
    checked_at: str = ""
    checked_note: str = ""


def _api_endpoint(section: Any) -> str:
    return f"{section.method} {section.url}"


def _list_path(list_mode: str, list_url: str, config: ApiConfig | None) -> str:
    """목록을 어디서 얻는지 한 줄. `api` 면 엔드포인트, 아니면 목록 주소다."""
    if list_mode != API:
        return list_url
    if config is None or config.list is None:
        return "목록 API 설정이 없다. 이 크롤러는 목록을 가져오지 못한다"
    return _api_endpoint(config.list)


def _detail_path(
    detail_mode: str,
    config: ApiConfig | None,
    selectors: SelectorSet | None,
    list_mode: str,
) -> tuple[str, str]:
    """상세로 가는 법. 낱말과 그 근거가 되는 실제 값을 함께 돌려준다."""
    if detail_mode == API:
        if config is None or config.detail is None:
            return DETAIL_API_WORD, "상세 API 설정이 없다. 이 크롤러는 상세를 가져오지 못한다"
        return DETAIL_API_WORD, _api_endpoint(config.detail)

    # 상세가 문서다. 그 주소를 항목에서 어떻게 얻는지가 남은 갈림길이다
    if list_mode == API:
        if config is None or config.list is None:
            return UNKNOWN_PATH_WORD, "목록 API 설정이 없어 상세 주소를 만들 수 없다"
        return DETAIL_TEMPLATE_WORD, config.list.link_template
    if selectors is None:
        return UNKNOWN_PATH_WORD, "셀렉터를 읽지 못했다. 셀렉터 편집에서 확인한다"
    if selectors.list.link_template.strip():
        return DETAIL_TEMPLATE_WORD, selectors.list.link_template
    if selectors.list.link.strip():
        return DETAIL_LINK_WORD, f"항목의 {selectors.list.link} 가 가리키는 주소"
    return DETAIL_NONE_WORD, "목록 항목에 상세로 갈 값이 없다. 본문은 채워지지 않는다"


def describe_path(
    *,
    list_mode: str,
    detail_mode: str,
    list_url: str = "",
    api_config_json: str | None = None,
    selectors_json: str | None = None,
    checked_at: str = "",
    checked_note: str = "",
) -> PathView:
    """저장된 값 하나로 경로를 낱말로 옮긴다. 읽지 못한 설정은 그 사실을 적는다.

    설정이 깨져 있어도 화면은 뜬다. 못 읽었다는 사실이 낱말 자리에 그대로 적히고, 그것이
    "설정이 없다" 와 "화면이 안 그린다" 를 가른다.
    """
    try:
        config: ApiConfig | None = parse_api_config(api_config_json)
    except ApiConfigError as exc:
        config = None
        broken = f"API 설정을 읽지 못했다: {exc}"
        return PathView(
            list_mode=list_mode,
            list_word=LIST_WORDS.get(list_mode, list_mode),
            list_note=broken if list_mode == API else list_url,
            detail_mode=detail_mode,
            detail_word=UNKNOWN_PATH_WORD,
            detail_note=broken,
            checked_at=checked_at,
            checked_note=checked_note,
        )

    selectors: SelectorSet | None = None
    if selectors_json:
        try:
            selectors = validate_selectors(json.loads(selectors_json))
        except (json.JSONDecodeError, SelectorSchemaError):
            selectors = None

    detail_word, detail_note = _detail_path(detail_mode, config, selectors, list_mode)
    return PathView(
        list_mode=list_mode,
        list_word=LIST_WORDS.get(list_mode, list_mode),
        list_note=_list_path(list_mode, list_url, config),
        detail_mode=detail_mode,
        detail_word=detail_word,
        detail_note=detail_note,
        checked_at=checked_at,
        checked_note=checked_note,
    )


# 라우트가 자기 조각에 직접 렌더하는 실패에도 같은 문구가 붙게 한다
templates.env.globals["next_step"] = next_step
templates.env.globals["mode_word"] = mode_word
templates.env.globals["reason_word"] = reason_word
templates.env.filters["as_time"] = format_time
templates.env.filters["tidy_text"] = collapse_blank_lines


def render_error(request: Request, reason: str, message: str) -> HTMLResponse:
    """실패 조각. 200 으로 돌려준다 — 4xx·5xx 는 HTMX 가 갈아 끼우지 않아 화면이 조용해진다."""
    return templates.TemplateResponse(
        request,
        "fragments/error.html",
        {"error": {"reason": reason, "message": message, "next": NEXT_STEPS.get(reason, "")}},
    )


def _detail_text(detail: Any) -> tuple[str, str]:
    if isinstance(detail, dict):
        return str(detail.get("reason", "server_error")), str(detail.get("message", detail))
    return "invalid_input", str(detail)


def install_ui_error_handlers(app: FastAPI) -> None:
    """`/ui/` 조각 요청의 실패를 200 과 오류 조각으로 바꾼다.

    라우트마다 이미 잡아 둔 실패는 그 자리에서 렌더된다. 여기는 그물이다 — 폼 검증(422),
    예상 못 한 예외(500)처럼 라우트를 지나쳐 버리는 실패가 화면에 아무것도 남기지 않는 것을
    막는다. `/api/...` 의 상태 코드는 그대로다. 소비 측과 스크립트가 그것을 읽는다.
    """

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> Response:
        if not request.url.path.startswith("/ui/"):
            return await request_validation_exception_handler(request, exc)
        fields = ", ".join(str(error.get("loc", ("",))[-1]) for error in exc.errors())
        return render_error(request, "invalid_input", f"보낸 값을 읽지 못했다: {fields}")

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException) -> Response:
        if not request.url.path.startswith("/ui/"):
            return await http_exception_handler(request, exc)
        reason, message = _detail_text(exc.detail)
        return render_error(request, reason, message)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> Response:
        if not request.url.path.startswith("/ui/"):
            raise exc
        # 진짜 서버 결함이다. 화면에는 사유를 남기고, 자세한 것은 로그에 남는다
        return render_error(request, "server_error", f"{type(exc).__name__}: {exc}")


@router.get("/crawlers", response_class=HTMLResponse)
def crawlers_page(request: Request) -> HTMLResponse:
    return render_page(request, "pages/crawlers.html")


@router.get("/tests", response_class=HTMLResponse)
def tests_page(request: Request) -> HTMLResponse:
    return render_page(request, "pages/tests.html")


@router.get("/workflows", response_class=HTMLResponse)
def workflows_page(request: Request) -> HTMLResponse:
    return render_page(request, "pages/workflows.html")


@router.get("/side", response_class=HTMLResponse)
def side_page(request: Request) -> HTMLResponse:
    """부가 워크플로우 화면. 크롤링과 따로 도는 작업을 여기서 등록하고 돌린다.

    `/workflows` 와 같은 층이다. 운영 설정의 하위로 넣지 않는 이유는 여기에 등록·주기·실행·
    이력이 다 있기 때문이다 — 값을 한 번 넣어 두는 화면이 아니라 운영하는 화면이다.
    """
    return render_page(request, "pages/side.html")


@router.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request) -> HTMLResponse:
    return render_page(request, "pages/rules.html")


@router.get("/taxonomy", response_class=HTMLResponse)
def taxonomy_page(request: Request) -> HTMLResponse:
    """직무 분류 체계 화면. `/rules` 와 같은 묶음이다 — 분류 체계는 정규화 파이프라인의
    입력이지 수집이 아니다."""
    return render_page(request, "pages/taxonomy.html")


@router.get("/prompt-rules", response_class=HTMLResponse)
def prompt_rules_page(request: Request) -> HTMLResponse:
    """분류 AI 에게 주는 칸별·공통 규칙 화면. 직무 분류와 같은 묶음이다 — 둘 다 분류의 입력이다."""
    return render_page(request, "pages/prompt_rules.html")


@router.get("/companies", response_class=HTMLResponse)
def companies_page(request: Request) -> HTMLResponse:
    """회사 화면. 받는 형식과 상한은 저장소 모듈에서 가져와 적는다 — 두 곳에서 따로 쓰지 않는다."""
    return render_page(
        request,
        "pages/companies.html",
        accepted=s3.ACCEPTED,
        max_label=s3.MAX_IMAGE_LABEL,
    )


@router.get("/jobs")
def jobs_page() -> RedirectResponse:
    """옛 데이터 조회 주소. 합쳐진 데이터 검수 화면으로 보낸다 (Push 30).

    두 화면이 같은 데이터를 두 벌로 보여주던 것을 하나로 합쳤다. 주소를 없애 404 로 두면
    운영자의 북마크와 지난 작업 기록의 링크가 전부 죽는다.

    영구 이동(301·308)으로 두지 않는다. 브라우저가 그것을 캐시하면 주소를 되돌릴 때 사용자
    쪽에서 지울 방법이 없다.
    """
    return RedirectResponse("/review", status_code=307)


# 운영 설정의 하위 메뉴. 한 화면에 다섯 구역이 있으면 찾지 못한다.
# 위쪽 네비게이션과 달리 여기는 `/settings` 하나로 묶여 있어서, 어느 하위 화면에 있든
# 위 네비게이션은 `운영 설정` 이 켜져 있어야 한다
SETTINGS_NAV: tuple[tuple[str, str], ...] = (
    ("/settings", "AI 제공자"),
    ("/settings/notify", "알림"),
    ("/settings/storage", "파일 저장소"),
    ("/settings/runs", "동시 실행"),
    ("/settings/export", "스냅샷 내보내기"),
    ("/settings/import", "데이터 가져오기"),
)


def render_settings(request: Request, name: str, /) -> HTMLResponse:
    """운영 설정의 하위 화면 하나. 위 네비게이션은 `/settings` 에 머문다."""
    return render_page(
        request,
        name,
        active="/settings",
        settings_nav=SETTINGS_NAV,
        settings_active=request.url.path,
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> HTMLResponse:
    return render_settings(request, "pages/settings_llm.html")


@router.get("/settings/notify", response_class=HTMLResponse)
def settings_notify_page(request: Request) -> HTMLResponse:
    return render_settings(request, "pages/settings_notify.html")


@router.get("/settings/storage", response_class=HTMLResponse)
def settings_storage_page(request: Request) -> HTMLResponse:
    return render_settings(request, "pages/settings_storage.html")


@router.get("/settings/runs", response_class=HTMLResponse)
def settings_runs_page(request: Request) -> HTMLResponse:
    return render_settings(request, "pages/settings_runs.html")


@router.get("/settings/export", response_class=HTMLResponse)
def settings_export_page(request: Request) -> HTMLResponse:
    return render_settings(request, "pages/settings_export.html")


@router.get("/settings/import", response_class=HTMLResponse)
def settings_import_page(request: Request) -> HTMLResponse:
    return render_settings(request, "pages/settings_import.html")


@router.get("/ui/health", response_class=HTMLResponse)
def health_fragment(request: Request) -> HTMLResponse:
    """모든 페이지 하단이 로드 직후 이 조각으로 갈린다. HTMX 가 붙었는지 화면에서 바로 보인다."""
    return render(
        request,
        "fragments/health.html",
        checked_at=datetime.now(UTC),
    )
