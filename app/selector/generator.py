"""Gemini API 로 셀렉터를 생성한다.

`.claude/rules/llm.md` 가 정한 것을 그대로 담는다.

- 보내는 것은 정제·샘플링된 HTML 뿐이다. 원본 페이지를 그대로 싣지 않는다
- 응답은 셀렉터 JSON 스키마로 강제하고, 받은 뒤에도 다시 검증한다
- 스키마에 맞지 않는 응답은 1회 재생성한다. 필수 칸이 0개 매칭이면 무엇이 틀렸는지 적어 1회 더
  묻는다. 그래도 틀린 칸은 운영자에게 넘긴다
- 생성마다 모델 ID, 입출력 토큰 수, 지연을 로그로 남긴다
- API 키는 환경변수에서만 읽고 어디에도 남기지 않는다

호출 자체는 고른 제공자 항목이 한다 (`app/llm/`). 이 파일은 셀렉터 생성에만 있는 것 —
프롬프트, 응답 스키마, 자체 검증 — 만 갖고, **어느 제공자인지 모른다.** 기능마다 다른
제공자를 고를 수 있고 그 선택은 설정이 정한다 (`app/llm/providers.py`).

페이지를 가져오는 것은 공용 fetch 클라이언트다 (`.claude/rules/crawling.md`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any

from bs4 import BeautifulSoup

from app.config import Settings, get_settings
from app.crawler.fetcher import PageSource, get_fetcher
from app.crawler.playwright import STATIC
from app.llm.base import LlmCallError, Provider, Usage
from app.llm.log import SELECTOR_GENERATE
from app.llm.providers import for_feature
from app.selector.cleaner import CleanedHtml, clean_html
from app.selector.narrow import Narrowing, narrow_item_selector
from app.selector.schema import (
    SelectorSchemaError,
    SelectorSet,
    parse_generated,
    parse_generated_allowing_empty,
)
from app.selector.verify import VerificationReport, verify_selectors

logger = logging.getLogger(__name__)

# 스키마에 맞지 않는 응답은 두 번까지 묻는다 (`.claude/rules/llm.md`). 0개 매칭으로 다시 묻는
# 한 번은 따로다 — 한 생성의 호출은 많아야 세 번이다
MAX_ATTEMPTS = 2

_SYSTEM_INSTRUCTION = (
    "너는 채용공고 페이지에서 CSS 셀렉터를 뽑는다. "
    "셀렉터는 BeautifulSoup 의 select() 로 그대로 쓸 수 있어야 한다. "
    "본 적 없는 클래스명을 지어내지 말고, 주어진 HTML 에 실제로 있는 것만 쓴다."
)

_PROMPT = """아래는 한 채용 사이트의 목록 페이지와 상세 페이지 HTML 이다.
script, style, 주석은 이미 걷어냈고 반복되는 목록 항목은 앞의 몇 개만 남겨 두었다.

규칙:
- list.item 은 공고 하나에 해당하는 반복 요소다. 목록 전체를 감싸는 컨테이너가 아니다.
- list.title, list.link, list.date 는 list.item 안에서 찾을 수 있는 셀렉터로 쓴다.
- list.link 는 상세 페이지로 가는 a 태그를 가리켜야 한다. 그 a 의 href 가 실제 주소여야 한다.
- 항목 안에 그런 a 가 없거나 href 가 javascript: 나 # 뿐이면 list.link 를 빈 문자열로 둔다.
  제목이나 카드 같은 다른 요소를 링크 대신 고르지 않는다. 링크가 아닌 요소를 고르면 목록은
  읽히는데 상세로 갈 수 없어 실행이 통째로 실패한다.
- link_template 은 항상 빈 문자열로 둔다. 상세 URL 형식은 이 HTML 만으로 알 수 없고,
  필요하면 운영자가 채운다. 주소를 지어내지 않는다.
- detail.title 과 detail.body 는 반드시 채운다.
- detail.qualifications, detail.recruitment_end_at, detail.department 는 페이지에 해당 항목이 없으면
  빈 문자열로 둔다. 아무 요소나 억지로 고르지 않는다.
- detail.recruitment_start_at(모집 시작일), detail.job_category(직군), detail.employment_type(정규직
  /인턴/기간제), detail.experience_type(신입/경력), detail.region(근무지),
  detail.headcount(모집인원), detail.responsibilities(주요 업무),
  detail.preferred_qualifications(우대 조건), detail.hiring_process(전형 절차),
  detail.recruitment_notice(기타) 도 같다. **그 값만 따로 담은 요소가 있을 때만** 채우고, 본문 안에
  문장으로 섞여 있을 뿐이면 빈 문자열로 둔다. 본문 전체를 가리키는 셀렉터를 이 자리에 넣지 않는다 —
  그러면 같은 본문이 칸마다 반복된다.
- list.date 도 마찬가지다. 항목 안에 게시일이나 모집 기간이 보이지 않으면 빈 문자열로 둔다.
  날짜가 아닌 값을 날짜 자리에 넣지 않는다.
- 클래스명이 `css-1d3w5wq` 처럼 자동 생성된 해시로 보이면 고르지 않는다. 그런 이름은 페이지나
  배포마다 바뀌어 다음 실행에서 0개 매칭이 된다. 의미 있는 클래스명이나 구조(태그, 부모-자식
  관계)로 대신 잡는다.
- list.company_name 와 detail.company_name 는 그 공고를 낸 회사 이름이 적힌 요소다. 사이트 하나에
  여러 계열사 공고가 섞이는 경우가 있어서 공고마다 다른 값이 나올 수 있다.
- 회사 이름이 페이지에 없으면 list.company_name 와 detail.company_name 를 빈 문자열로 둔다.
  사이트 이름이나 로고 문구를 회사명으로 대신 고르지 않는다. 없는 것을 지어내면 잘못된
  회사명이 공고마다 붙는다.

[목록 페이지 {list_url}]
{list_html}

[상세 페이지 {detail_url}]
{detail_html}
"""


class SelectorGenerationError(RuntimeError):
    """생성 실패. `reason` 으로 무엇을 해야 할지가 갈린다.

    | reason | 다음 행동 |
    |---|---|
    | `no_api_key` | 환경변수를 채운다. 서버 문제가 아니다 |
    | `api_error` | Gemini 응답 자체가 실패했다. 잠시 뒤 다시 |
    | `unparsable` | 1회 재생성까지 하고도 JSON 이 아니었다. 손으로 쓴다 |
    | `unknown_field`, `missing_field` | 모델이 스키마를 벗어났다. 손으로 쓴다 |
    """

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class GenerationResult:
    """생성 결과. `verification.failed` 가 비어 있어야 성공이다.

    0개 매칭 필드가 있어도 예외를 던지지 않는다. 셀렉터 전체를 버리는 대신 실패한 필드 이름을
    운영자에게 보여 주는 편이 낫다 — 손으로 그 필드만 고치는 것이 첫 수단이다
    (`.claude/rules/llm.md`).
    """

    selectors: SelectorSet
    usage: Usage
    attempts: int
    verification: VerificationReport
    notes: list[str] = field(default_factory=list)
    # 이 셀렉터를 어느 경로로 가져온 HTML 에서 만들었는가. `static` 또는 `playwright` 이고,
    # 채우는 것은 HTML 을 가져온 쪽이다 (`app/api/crawlers.py` 의 `get_generator`).
    # 판정이 경로를 알아내지 못했을 때 되돌아갈 값이 이것이다
    render_mode: str = STATIC

    @property
    def ok(self) -> bool:
        return self.verification.ok


async def generate_for_urls(
    list_url: str,
    detail_url: str,
    *,
    source: PageSource | None = None,
    settings: Settings | None = None,
    client: Any | None = None,
) -> GenerationResult:
    """두 URL 을 가져와 셀렉터를 생성한다.

    `source` 는 정적 fetch 클라이언트이거나 렌더러다. 어느 쪽인지는 `crawlers.render_mode` 를
    읽는 호출부가 정하고, 여기서는 가져온 HTML 만 본다. JS 로 그려지는 사이트는 정적 HTML 에
    목록이 없어서, 렌더된 HTML 이 아니면 셀렉터를 만들 근거 자체가 없다.
    """
    resolved_source = source or get_fetcher()
    list_html = (await resolved_source.fetch(list_url)).text
    detail_html = (await resolved_source.fetch(detail_url)).text
    return await generate_from_html(
        list_html,
        detail_html,
        list_url=list_url,
        detail_url=detail_url,
        settings=settings,
        client=client,
    )


async def generate_from_html(
    list_html: str,
    detail_html: str,
    *,
    list_url: str = "",
    detail_url: str = "",
    settings: Settings | None = None,
    client: Any | None = None,
) -> GenerationResult:
    """이미 가져온 HTML 로 생성한다. 저장된 픽스처로 돌릴 수 있는 경로다."""
    resolved = settings or get_settings()
    provider, model = _chosen(SELECTOR_GENERATE, resolved)
    resolved_client = client or build_client(resolved)

    cleaned_list = clean_html(list_html)
    cleaned_detail = clean_html(detail_html)
    prompt = build_prompt(cleaned_list, cleaned_detail, list_url, detail_url)

    last_error: SelectorSchemaError | None = None
    last_text: str | None = None
    # 스키마 거절로 다시 물은 수와, 0개 매칭으로 다시 물은 적이 있는지. 둘은 따로 센다
    schema_failures = 0
    match_retried = False
    feedback = ""
    spent: Usage | None = None
    best: GenerationResult | None = None
    attempt = 0
    while True:
        attempt += 1
        text, usage = await call_model(
            resolved_client, model, prompt + feedback, attempt, provider=provider
        )
        spent = usage if spent is None else _added(spent, usage)
        last_text = text
        try:
            selectors, moved = parse_generated(text)
        except SelectorSchemaError as exc:
            logger.warning(
                "셀렉터 생성 응답 거절 model=%s attempt=%d reason=%s message=%s",
                model,
                attempt,
                exc.reason,
                exc,
            )
            # 깨진 모양(`unparsable`), 모자란 내용(`missing_field`), 스키마에 없는 이름
            # (`unknown_field`) 모두 한 번 더 묻는다. 없는 이름은 뜻을 추측해 살리지 않고 다시
            # 묻기만 한다 — 같은 응답이 두 번 오면 거절한다. 옆 묶음에 잘못 둔 칸은 거절하기 전에
            # `parse_generated` 가 제자리로 옮긴다 (2026-09-17)
            last_error = exc
            schema_failures += 1
            if schema_failures >= MAX_ATTEMPTS:
                break
            feedback = _schema_feedback(exc)
            continue

        # 항목 셀렉터가 공고가 아닌 반복까지 잡았으면 제목이 있는 쪽으로 좁힌다. 넓히지는
        # 않는다 (`app/selector/narrow.py`)
        narrowing = narrow_item_selector(selectors, list_html)
        selectors = narrowing.selectors
        # 방금 가져온 그 HTML 에 즉시 적용한다. 정제 전 원본이라 샘플링으로 덜어낸 항목도 본다.
        report = verify_selectors(selectors, list_html, detail_html)
        logger.info(
            "셀렉터 자체 검증 model=%s 매칭=%s 실패=%s",
            model,
            report.summary(),
            report.failed or "없음",
        )
        result = GenerationResult(
            selectors=selectors,
            usage=spent,
            attempts=attempt,
            verification=report,
            notes=[*_notes(cleaned_list, cleaned_detail, narrowing), *moved],
        )
        missed = _problems(report, selectors, list_html, detail_html)
        if not missed:
            return result
        if best is None or len(missed) < len(
            _problems(best.verification, best.selectors, list_html, detail_html)
        ):
            best = result
        if match_retried:
            break
        # 모양은 맞는데 꼭 있어야 할 칸이 이 HTML 에서 0개다. 같은 질문을 되풀이하지 않고 무엇이
        # 틀렸는지 적어 한 번만 더 묻는다. DeepSeek 가 클래스 앞의 `.` 을 빼거나(이노션) 목록을
        # 비워 답한 것(HD현대)이 이렇게 풀렸다 (2026-09-22)
        match_retried = True
        feedback = _match_feedback(missed)

    if best is not None:
        # 다시 물어도 모든 칸이 맞지 않았다. 덜 틀린 답을 draft 로 남기고 틀린 칸은 화면이 알린다
        return _without_number_date(
            replace(best, usage=spent or best.usage, attempts=attempt), list_html
        )

    assert last_error is not None  # 루프는 최소 한 번 돈다

    if last_error.reason == "missing_field" and last_text is not None:
        # 모양은 맞는데 필드가 비어 있다. 통째로 버리면 운영자가 손으로 고칠 대상조차 없다.
        # 빈 채로 draft 에 저장하고 어느 자리가 비었는지 알린다 (`.claude/rules/llm.md`).
        selectors, empty_fields, moved = parse_generated_allowing_empty(last_text)
        narrowing = narrow_item_selector(selectors, list_html)
        selectors = narrowing.selectors
        report = verify_selectors(selectors, list_html, detail_html)
        logger.warning(
            "셀렉터 생성 필드 누락 model=%s attempts=%d 빈 필드=%s",
            model,
            MAX_ATTEMPTS,
            ", ".join(empty_fields),
        )
        return GenerationResult(
            selectors=selectors,
            usage=spent or usage,
            attempts=attempt,
            verification=report,
            notes=[
                *_notes(cleaned_list, cleaned_detail, narrowing),
                *moved,
                *(
                    [f"모델이 채우지 못한 필드: {', '.join(empty_fields)}. 손으로 채운다"]
                    if empty_fields
                    else []
                ),
            ],
        )

    # 스키마에 없는 이름이 끝까지 왔으면 그 사유를 그대로 둔다. 화면이 사유마다 다음 할 일을 적는다
    reason = "unknown_field" if last_error.reason == "unknown_field" else "unparsable"
    raise SelectorGenerationError(
        reason, f"{MAX_ATTEMPTS}회 모두 스키마에 맞지 않았다: {last_error}"
    ) from last_error


# 생성 직후 검증에서 0개면 다시 묻는 칸. 목록은 항목과 제목, 상세는 페이지를 봤을 때만
# 제목과 본문이다. 링크·날짜는 없는 사이트가 있어 넣지 않는다
_REQUIRED_LIST = ("list.item", "list.title")
_REQUIRED_DETAIL = ("detail.title", "detail.body")
# 날짜 칸을 볼 항목 수
_DATE_SAMPLE = 20


def _required_misses(report: VerificationReport, detail_html: str) -> list[str]:
    required = _REQUIRED_LIST + (_REQUIRED_DETAIL if detail_html.strip() else ())
    failed = set(report.failed)
    return [name for name in required if name in failed]


def _schema_feedback(exc: SelectorSchemaError) -> str:
    hint = (
        " 목록 HTML 안에서 공고 하나에 해당하는 반복 요소를 찾아 그 칸을 채운다."
        if exc.reason == "missing_field"
        else ""
    )
    return f"\n\n[직전 답이 거절됐다]\n{exc}.{hint} 스키마를 지켜 다시 답한다.\n"


def _problems(
    report: VerificationReport, selectors: SelectorSet, list_html: str, detail_html: str
) -> list[str]:
    """생성 직후 다시 물을 거리. 한 줄이 한 칸이고, 모델에게 그대로 보인다."""
    by_name = {field.name: field for field in report.fields}
    lines = [
        f"- {name}: `{by_name[name].selector}` 가 0개 매칭이었다"
        for name in _required_misses(report, detail_html)
    ]
    sample = _number_only_date(selectors, list_html)
    if sample:
        lines.append(
            f"- list.date: `{selectors.list.date}` 가 고른 값이 날짜가 아니라 숫자뿐이다(예: "
            f"{sample}). 조회수나 순번 칸이다. 모집 기간이나 마감일이 적힌 칸을 고르고, 없으면 "
            "빈 문자열로 둔다"
        )
    return lines


def _number_only_date(selectors: SelectorSet, list_html: str) -> str:
    """목록 날짜 칸이 잡은 값이 전부 숫자뿐이면 그 첫 값. 아니면 빈 문자열.

    카페스 실측(2026-09-22): 모델이 날짜로 표의 마지막 칸(조회수)을 골랐다. 0개 매칭이 아니라
    검증을 통과했고, 마감 거르기가 조회수를 날짜로 읽지 못해 끝난 공고까지 상세를 열었다.
    """
    if not selectors.list.item or not selectors.list.date:
        return ""
    try:
        nodes = BeautifulSoup(list_html, "html.parser").select(selectors.list.item)
        found = [node.select_one(selectors.list.date) for node in nodes[:_DATE_SAMPLE]]
    except Exception:  # 셀렉터 문법 오류는 검증이 따로 적는다
        return ""
    texts = [node.get_text(" ", strip=True) for node in found if node is not None]
    texts = [text for text in texts if text]
    if texts and all(text.replace(",", "").isdigit() for text in texts):
        return texts[0]
    return ""


def _without_number_date(result: GenerationResult, list_html: str) -> GenerationResult:
    """다시 물어도 날짜 칸이 숫자뿐이면 비운다. 틀린 날짜보다 없는 날짜가 낫다."""
    sample = _number_only_date(result.selectors, list_html)
    if not sample:
        return result
    cleared = result.selectors.model_copy(
        update={"list": result.selectors.list.model_copy(update={"date": ""})}
    )
    return replace(
        result,
        selectors=cleared,
        notes=[
            *result.notes,
            f"목록 날짜 칸이 날짜가 아니라 숫자({sample})를 잡아 비웠다. 필요하면 손으로 채운다",
        ],
    )


def _match_feedback(lines: list[str]) -> str:
    body = "\n".join(lines)
    return (
        "\n\n[직전 답을 위 HTML 에 적용해 봤다]\n"
        f"다음 칸이 틀렸다.\n{body}\n"
        "CSS 셀렉터 문법을 지킨다 — 클래스는 `.이름`, id 는 `#이름` 이다. 위 HTML 에 실제로 있는 "
        "요소로 다시 고르고, 맞았던 칸은 그대로 둔다.\n"
    )


def _added(first: Usage, second: Usage) -> Usage:
    """여러 번 부른 생성의 비용을 합친다. 비용 질문에 답하려면 모두 세야 한다."""
    return Usage(
        provider=first.provider,
        model=first.model,
        input_tokens=first.input_tokens + second.input_tokens,
        output_tokens=first.output_tokens + second.output_tokens,
        total_tokens=first.total_tokens + second.total_tokens,
        latency_ms=first.latency_ms + second.latency_ms,
    )


def build_client(settings: Settings | None = None) -> Any:
    """셀렉터 생성이 쓰는 제공자의 클라이언트."""
    return build_client_for(SELECTOR_GENERATE, settings)


def build_client_for(feature: str, settings: Settings | None = None) -> Any:
    """그 기능이 고른 제공자의 클라이언트. API 키는 설정에서만 온다.

    생성과 고치기가 서로 다른 제공자를 고를 수 있어서 기능을 받는다. 어느 제공자인지는
    여기서 알 필요가 없다 — 이름을 항목으로 바꾸는 일은 `app/llm/providers.py` 가 한다.
    """
    resolved = settings or get_settings()
    try:
        provider, _ = for_feature(feature, resolved)
        return provider.build_client(resolved)
    except LlmCallError as exc:
        raise SelectorGenerationError(
            exc.reason, f"{exc}. 서버 문제가 아니라 셀렉터 생성만 막힌다"
        ) from exc


def _chosen(feature: str, settings: Settings) -> tuple[Provider, str]:
    try:
        return for_feature(feature, settings)
    except LlmCallError as exc:
        raise SelectorGenerationError(exc.reason, str(exc)) from exc


def build_prompt(
    cleaned_list: CleanedHtml,
    cleaned_detail: CleanedHtml,
    list_url: str = "",
    detail_url: str = "",
) -> str:
    return _PROMPT.format(
        list_url=list_url,
        detail_url=detail_url,
        list_html=cleaned_list.html,
        detail_html=cleaned_detail.html,
    )


async def call_model(
    client: Any,
    model: str,
    prompt: str,
    attempt: int,
    kind: str = "셀렉터 생성",
    *,
    provider: Provider,
) -> tuple[str, Usage]:
    """셀렉터용 호출 1회. 스키마와 시스템 지시만 이 파일 것이고 나머지는 공용이다.

    생성과 고치기가 같은 함수를 쓴다. 두 번째 API 경로를 만들면 로그도 재시도 규칙도 두 벌이
    되고, 한쪽만 고쳐진 채로 남는다. `kind` 는 로그에서 둘을 가르는 이름일 뿐이다.
    """
    try:
        return await provider.call_model(
            client,
            model,
            prompt,
            attempt,
            kind,
            response_schema=SelectorSet,
            system_instruction=_SYSTEM_INSTRUCTION,
        )
    except LlmCallError as exc:
        raise SelectorGenerationError(exc.reason, str(exc)) from exc


def _notes(
    cleaned_list: CleanedHtml, cleaned_detail: CleanedHtml, narrowing: Narrowing | None = None
) -> list[str]:
    """입력을 좁혔거나 잘랐으면 응답에 남긴다 (`.claude/rules/llm.md`).

    항목 셀렉터를 좁힌 것도 같이 적는다. 모델이 낸 것과 저장되는 것이 다르면 그 사실이
    운영자에게 보여야 한다.
    """
    notes = [f"목록: {note}" for note in cleaned_list.notes()] + [
        f"상세: {note}" for note in cleaned_detail.notes()
    ]
    if narrowing is not None and narrowing.note:
        notes.append(narrowing.note)
    return notes
