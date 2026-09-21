"""등록 판정이 막힌 자리에서, 모은 증거를 AI 에게 보여 목록 경로를 제안받는다.

판정(`app/selector/discovery.py`)은 규칙으로 돈다. 렌더하고, 누르고, 관찰한 요청을 다시 불러
본다. 규칙으로 풀리지 않는 것은 **판단**이다 — 2026-09-21 에 HD현대와 동원을 손으로 등록하며
사람이 한 일이 그랬다.

| 사이트 | 사람이 한 판단 |
|---|---|
| HD현대 | 목록 API 의 `receiveEndDatetime` 이 마감일이다 |
| 동원 | robots 가 `page=` 를 막으니 빼고 `countPerPage` 를 키워 한 번에 받는다 |

여기서는 그 판단을 AI 에게 맡기되, **답을 믿지 않는다.** 제안은 셋을 다 통과해야 쓴다.

1. 공용 fetch 클라이언트로 제안한 주소를 부른다. robots 도 여기서 걸린다
2. 받은 목록에 렌더된 제목이 들어 있다 (`app/selector/list_api.py` 와 같은 기준)
3. **실제로 알고 있는 공고 주소가 제안한 형식으로 똑같이 나온다.** 눌러서 도착한 주소나 항목이
   들고 있던 주소다. 형식을 지어내면 여기서 떨어진다

마감일 칸은 한 번 더 본다. 지금 화면에 떠 있는 공고는 진행 중이므로, 그 공고들의 날짜가
오늘보다 이전이면 그 칸은 마감일이 아니다(게시일·접수 시작일). 그때는 칸은 두고 마감일로
보지는 않는다 — 마감일로 잘못 보면 어제 올라온 공고가 조용히 버려진다.

## 부르는 때

판정이 막혔을 때만 부른다. 규칙으로 풀린 사이트에 호출 비용을 쓰지 않는다.

- 공고 한 건은 열었는데 나머지 공고로 갈 길이 없다 (동원)
- 목록 API 는 채택했는데 마감일 칸을 읽지 못했다 (HD현대)

## 입력은 잘라서 보낸다

관찰한 응답은 통째로 보내지 않는다. HD현대 목록 응답이 1.4MB 다. 배열마다 경로·길이와 항목
하나를 `경로: 값` 줄로 편 골격만 보내고, 값도 자른다 (`.claude/rules/llm.md`).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ValidationError

from app.config import Settings
from app.crawler.api_source import fetch_list
from app.crawler.fetcher import FetchError, FetchPolicy, RobotsDisallowedError
from app.crawler.parser import CrawlDataError, ListItem
from app.crawler.playwright import ObservedRequest, functional_headers
from app.llm.base import LlmCallError, Usage
from app.llm.log import SELECTOR_GENERATE
from app.llm.providers import for_feature
from app.selector.api_schema import ApiConfig, ApiConfigError, ApiListConfig, validate_api_config
from app.selector.list_api import ListPath

logger = logging.getLogger(__name__)

# 보내는 증거의 상한. 요청 수, 요청 하나에서 보여 줄 배열 수, 항목을 편 줄 수, 값 길이
MAX_REQUESTS = 6
MAX_ARRAYS = 3
MAX_LINES = 40
VALUE_CHARS = 80
BODY_CHARS = 300
ROBOTS_CHARS = 1500
MAX_TITLES = 10
FLATTEN_DEPTH = 4

# 목록으로 볼 배열의 최소 길이. `app/selector/list_api.py` 와 같은 값이다
MIN_ENTRIES = 2
# 제목이 맞아야 하는 최소 건수. `app/selector/list_api.py` 와 같은 기준이다
MIN_TITLE_HITS = 2

_DATE = re.compile(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})")

SYSTEM_INSTRUCTION = (
    "너는 채용공고 크롤러의 등록을 돕는다. 브라우저가 관찰한 요청과 응답 골격을 보고, 크롤러가 "
    "브라우저 없이 공고 목록을 받을 수 있는 API 설정을 제안한다. 증거에 없는 값을 지어내지 않는다."
)

PROMPT = """목록 페이지 {list_url} 를 브라우저로 열었을 때 나간 요청들이다.
크롤러는 브라우저 없이 HTTP 로 목록을 받아야 한다. 공고 목록을 담은 요청을 골라 설정을 제안한다.

## 화면에 그려진 공고 제목
{titles}

## 실제로 확인한 공고 주소 (제목 → 주소)
{known_links}

## robots.txt
{robots}

## 관찰한 JSON 요청
{requests}

## 규칙
- `url` 은 크롤러가 부를 주소다. robots.txt 가 막는 주소는 쓰지 않는다. 막힌 이유가 쪽 번호 같은
  쿼리 파라미터 하나라면, 그 파라미터를 빼고 한 쪽에 담는 개수 파라미터를 키워 한 번에 다 받는
  주소를 제안해도 된다. 관찰한 요청과 같은 호스트만 쓴다.
- `items_path` 는 공고 배열의 점 경로다. `title_field`·`date_field`·`company_field`·`id_field` 는
  배열 항목 안의 점 경로다. 없는 칸은 빈 문자열이다.
- `link_template` 은 `{{id}}` 를 반드시 포함하고, 위 "실제로 확인한 공고 주소" 와 글자 하나 다르지
  않게 나와야 한다. `id_field` 의 값을 `{{id}}` 에 넣었을 때 그 주소가 된다.
- `date_field` 는 마감일 칸이 있으면 그것을 고른다. 상시채용·채용 시 마감 공고라서 일부 항목의
  마감일이 null 이어도 마감일 칸을 고른다. 마감일 칸이 아예 없을 때만 게시일 같은 다른 날짜를 쓴다.
- `date_is_deadline` 은 `date_field` 가 **마감일**일 때만 true 다.
  게시일·접수 시작일·수정일이면 false.
- `headers` 는 관찰한 요청에 붙어 있던 헤더 중 응답을 받는 데 필요한 것만 적는다.
- `body_json` 은 POST 본문 JSON 문자열이다. GET 이면 빈 문자열이다.
- 목록을 담은 요청이 없거나 확신이 없으면 `found` 를 false 로 두고 이유를 적는다.
"""


class ProposedHeader(BaseModel):
    name: str
    value: str


class PathAnswer(BaseModel):
    """AI 의 답. 모든 칸이 필수인 평평한 모양이다.

    strict 도구 호출이 자유 키 사전을 받지 않아 헤더도 (이름, 값) 목록으로 받는다.
    """

    found: bool
    url: str
    method: str
    body_json: str
    items_path: str
    title_field: str
    date_field: str
    date_is_deadline: bool
    company_field: str
    id_field: str
    link_template: str
    headers: list[ProposedHeader] = Field(default_factory=list)
    reason: str


@dataclass(frozen=True)
class PathEvidence:
    """판정이 모은 것. 프롬프트도 확인도 이것 하나로 한다."""

    list_url: str
    items: Sequence[ListItem]
    # 실제로 확인한 (제목, 주소). 눌러서 도착한 주소나 항목이 들고 있던 주소다
    known_links: Sequence[tuple[str, str]]
    requests: Sequence[ObservedRequest]
    robots: str = ""


@dataclass(frozen=True)
class Proposal:
    """AI 에게 물은 결과. `path` 가 있으면 세 확인을 다 통과한 것이다."""

    path: ListPath | None
    note: str
    usage: Usage | None = None
    answer: PathAnswer | None = None


# 판정이 부르는 모양. 테스트는 모델 대신 답을 돌려주는 함수를 끼운다
PathAsker = Callable[[str], Awaitable[tuple[PathAnswer, Usage | None]]]


def build_prompt(evidence: PathEvidence) -> str:
    """증거를 프롬프트로. 응답은 골격만, 값은 잘라서 넣는다."""
    titles = [item.title for item in evidence.items if item.title.strip()][:MAX_TITLES]
    return PROMPT.format(
        list_url=evidence.list_url,
        titles="\n".join(f"- {title}" for title in titles) or "(없음)",
        known_links="\n".join(f"- {title} → {url}" for title, url in evidence.known_links)
        or "(없음)",
        robots=evidence.robots[:ROBOTS_CHARS].strip() or "(받지 못했다)",
        requests="\n\n".join(_describe_requests(evidence)) or "(없음)",
    )


async def propose_path(
    ask: PathAsker, fetcher: FetchPolicy, evidence: PathEvidence, *, today: date | None = None
) -> Proposal:
    """물어보고 확인한다. 어느 단계에서 떨어졌는지를 `note` 에 적는다."""
    try:
        answer, usage = await ask(build_prompt(evidence))
    except LlmCallError as exc:
        return Proposal(path=None, note=f"AI 에게 경로를 묻지 못했다: {exc}")

    if not answer.found:
        return Proposal(
            path=None,
            note=f"AI 도 목록 경로를 찾지 못했다: {answer.reason}",
            usage=usage,
            answer=answer,
        )
    try:
        path, note = await verify(fetcher, answer, evidence, today=today)
    except ProposalRejected as exc:
        return Proposal(
            path=None,
            note=f"AI 가 제안한 목록 경로({answer.url})는 확인되지 않아 버렸다: {exc}",
            usage=usage,
            answer=answer,
        )
    return Proposal(path=path, note=note, usage=usage, answer=answer)


class ProposalRejected(ValueError):
    """제안이 확인을 통과하지 못했다. 사유가 그대로 판정 근거에 들어간다."""


async def verify(
    fetcher: FetchPolicy,
    answer: PathAnswer,
    evidence: PathEvidence,
    *,
    today: date | None = None,
) -> tuple[ListPath, str]:
    """제안을 설정으로 옮기고 세 가지를 확인한다. 떨어지면 `ProposalRejected` 다."""
    config = _config(answer, evidence)

    try:
        fetched = await fetch_list(fetcher, config)
    except RobotsDisallowedError as exc:
        raise ProposalRejected(f"{exc}. 제안도 robots 가 막은 주소다") from exc
    except FetchError as exc:
        raise ProposalRejected(f"공용 fetch 클라이언트로 부르지 못했다: {exc}") from exc
    except CrawlDataError as exc:
        raise ProposalRejected(f"부른 응답에서 항목을 읽지 못했다: {exc}") from exc

    # 같은 제목이 여러 건일 수 있다. HD현대 목록 556건에는 지난 차수의 같은 제목 공고가 있다
    by_title: dict[str, list[ListItem]] = {}
    for item in fetched.items:
        by_title.setdefault(_squeeze(item.title), []).append(item)
    expected = [_squeeze(item.title) for item in evidence.items if item.title.strip()]
    matched = sum(1 for title in expected if title in by_title)
    if matched < min(MIN_TITLE_HITS, len(expected)) or matched * 2 < min(
        len(expected), len(fetched.items)
    ):
        raise ProposalRejected(
            f"받은 목록 {len(fetched.items)}건 중 화면의 제목과 같은 것이 {matched}건뿐이다"
        )

    checked = 0
    for title, url in evidence.known_links:
        same = by_title.get(_squeeze(title), [])
        if not same:
            continue
        if any(_same_url(item.link, url) for item in same):
            checked += 1
            continue
        raise ProposalRejected(
            f"`{title}` 의 주소가 {same[0].link} 로 나온다. 실제로 확인한 주소는 {url} 다"
        )
    if checked == 0:
        raise ProposalRejected("실제로 확인한 공고 주소와 대조할 항목이 받은 목록에 없다")

    notes = [
        f"AI 제안을 확인해 채택했다: {config.url} 의 `{config.items_path}` "
        f"({len(fetched.items)}건 중 제목 {matched}건 일치, 확인한 공고 주소 {checked}건 일치)",
        f"AI 가 든 이유: {answer.reason}",
    ]
    if config.date_is_deadline:
        doubt = _deadline_doubt(by_title, expected, today or date.today())
        if doubt:
            config = config.model_copy(update={"date_is_deadline": False})
            notes.append(f"`{config.fields.get('date')}` 은 마감일로 보지 않는다: {doubt}")
        else:
            notes.append(f"`{config.fields.get('date')}` 을 마감일로 본다")

    path = ListPath(
        api=ApiConfig(list=config),
        url=config.url,
        items_path=config.items_path,
        count=len(fetched.items),
        notes=tuple(notes),
        missing=tuple(name for name in ("date", "company_name") if name not in config.fields),
    )
    return path, ". ".join(notes)


def llm_asker(settings: Settings, client: Any | None = None) -> PathAsker:
    """등록할 때 셀렉터를 만드는 AI 로 묻는다. 경로 제안도 등록의 한 부분이라 같은 설정을 쓴다."""

    async def ask(prompt: str) -> tuple[PathAnswer, Usage | None]:
        provider, model = for_feature(SELECTOR_GENERATE, settings)
        text, usage = await provider.call_model(
            client or provider.build_client(settings),
            model,
            prompt,
            1,
            "경로 제안",
            response_schema=PathAnswer,
            system_instruction=SYSTEM_INSTRUCTION,
        )
        try:
            return PathAnswer.model_validate_json(text), usage
        except ValidationError as exc:
            raise LlmCallError("invalid_response", f"경로 제안 답을 읽지 못했다: {exc}") from exc

    return ask


def _config(answer: PathAnswer, evidence: PathEvidence) -> ApiListConfig:
    """답을 설정으로. 호스트와 헤더는 여기서 좁힌다 — 답이 아무 주소나 부르게 두지 않는다."""
    allowed_hosts = {urlsplit(evidence.list_url).netloc} | {
        urlsplit(request.url).netloc for request in evidence.requests
    }
    host = urlsplit(answer.url).netloc
    if host not in allowed_hosts:
        raise ProposalRejected(f"관찰한 적 없는 호스트다: {host}")
    link_hosts = {urlsplit(url).netloc for _, url in evidence.known_links}
    if urlsplit(answer.link_template).netloc not in link_hosts:
        raise ProposalRejected(
            f"공고 주소 형식의 호스트가 확인한 공고와 다르다: {answer.link_template}"
        )

    body: Any = {}
    if answer.body_json.strip():
        try:
            body = json.loads(answer.body_json)
        except json.JSONDecodeError as exc:
            raise ProposalRejected(f"POST 본문이 JSON 이 아니다: {exc}") from exc
        if not isinstance(body, Mapping):
            raise ProposalRejected("POST 본문이 객체가 아니다")

    fields = {"title": answer.title_field}
    if answer.date_field.strip():
        fields["date"] = answer.date_field
    if answer.company_field.strip():
        fields["company_name"] = answer.company_field
    # 브라우저가 붙인 것과 신원(`cookie`·`authorization`)은 답에 있어도 떨어진다
    headers = functional_headers({header.name: header.value for header in answer.headers})

    try:
        config = validate_api_config(
            {
                "list": {
                    "url": answer.url,
                    "method": answer.method.upper() or "GET",
                    "body": dict(body),
                    "items_path": answer.items_path,
                    "fields": fields,
                    "id_field": answer.id_field,
                    "link_template": answer.link_template,
                    "headers": headers,
                    "date_is_deadline": answer.date_is_deadline and "date" in fields,
                }
            }
        )
    except ApiConfigError as exc:
        raise ProposalRejected(f"설정 형식 검증에 걸렸다({exc.reason}): {exc}") from exc
    return config.list_config()


def _deadline_doubt(
    by_title: Mapping[str, Sequence[ListItem]], expected: Sequence[str], today: date
) -> str:
    """화면에 떠 있는 공고의 날짜가 지났으면 그 칸은 마감일이 아니다. 문제 없으면 빈 문자열.

    제목이 같은 공고가 여럿이면 가장 늦은 날짜를 본다. 화면에 떠 있는 것은 그중 새 차수다 —
    HD현대 목록에는 5월에 끝난 같은 제목의 공고가 함께 온다.
    """
    read = 0
    for title in dict.fromkeys(expected):
        dates = [value for item in by_title.get(title, []) if (value := _date_of(item.date))]
        if not dates:
            continue
        read += 1
        latest = max(dates)
        if latest < today:
            return f"화면에 떠 있는 `{title}` 의 날짜 {latest.isoformat()} 가 이미 지났다"
    if read == 0:
        return "화면에 떠 있는 공고에서 날짜로 읽히는 값이 없다"
    return ""


def _date_of(value: str) -> date | None:
    found = _DATE.search(value)
    if found is None:
        return None
    try:
        return date(int(found[1]), int(found[2]), int(found[3]))
    except ValueError:
        return None


def _describe_requests(evidence: PathEvidence) -> list[str]:
    """JSON 요청마다 주소·헤더·본문과 배열 골격. 제목이 든 요청이 먼저다."""
    titles = {_squeeze(item.title) for item in evidence.items if item.title.strip()}
    known = {_squeeze(title) for title, _ in evidence.known_links}
    scored: list[tuple[int, int, str]] = []
    for index, request in enumerate(evidence.requests):
        if request.status != 200 or not request.is_json:
            continue
        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            continue
        arrays = sorted(
            _arrays(payload),
            key=lambda pair: -sum(1 for entry in pair[1] if _entry_title(entry, titles)),
        )[:MAX_ARRAYS]
        if not arrays:
            continue
        hits = sum(1 for _, entries in arrays for entry in entries if _entry_title(entry, titles))
        lines = [
            f"### 요청 {index}: {request.method} {request.url}",
            f"헤더: {dict(request.request_headers) or '없음'}",
        ]
        if request.request_body.strip():
            lines.append(f"본문: {request.request_body[:BODY_CHARS]}")
        for path, entries in arrays:
            lines.append(f"배열 `{path}` ({len(entries)}건). 첫 항목:")
            lines.extend(_flatten_lines(entries[0]))
            sample = next((entry for entry in entries if _entry_title(entry, known)), None)
            if sample is not None and sample is not entries[0]:
                lines.append("확인한 공고 주소의 항목:")
                lines.extend(_flatten_lines(sample))
        scored.append((-hits, index, "\n".join(lines)))
    return [text for _, _, text in sorted(scored)[:MAX_REQUESTS]]


def _arrays(payload: Any, prefix: str = "", depth: int = 0) -> Iterator[tuple[str, list[Any]]]:
    """(경로, 객체 배열). 배열 안의 배열은 보지 않는다."""
    if depth > FLATTEN_DEPTH or not isinstance(payload, Mapping):
        return
    for key, value in payload.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, list):
            if len(value) >= MIN_ENTRIES and all(isinstance(entry, Mapping) for entry in value):
                yield path, value
            continue
        yield from _arrays(value, path, depth + 1)


def _flatten_lines(entry: Any) -> list[str]:
    """항목 하나를 `경로: 값` 줄로. 긴 값은 자른다."""
    lines: list[str] = []
    for path, value in _flatten(entry):
        text = value if len(value) <= VALUE_CHARS else f"{value[:VALUE_CHARS]}…({len(value)}자)"
        lines.append(f"  {path}: {text}")
        if len(lines) >= MAX_LINES:
            lines.append("  …")
            break
    return lines


def _flatten(entry: Any, prefix: str = "", depth: int = 0) -> Iterator[tuple[str, str]]:
    if depth > FLATTEN_DEPTH:
        return
    if isinstance(entry, Mapping):
        for key, value in entry.items():
            yield from _flatten(value, f"{prefix}.{key}" if prefix else str(key), depth + 1)
    elif isinstance(entry, list):
        if entry and all(not isinstance(value, Mapping | list) for value in entry):
            yield prefix, ", ".join(str(value) for value in entry)
        elif entry:
            yield from _flatten(entry[0], f"{prefix}.0", depth + 1)
    elif entry is None:
        yield prefix, "null"
    else:
        yield prefix, str(entry)


def _entry_title(entry: Any, titles: set[str]) -> bool:
    return any(_squeeze(value) in titles for _, value in _flatten(entry))


def _same_url(left: str, right: str) -> bool:
    """끝 슬래시와 조각(`#...`)만 무시하고 같은 주소인가. 쿼리는 그대로 견준다."""

    def norm(url: str) -> str:
        parts = urlsplit(url.strip())
        return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}?{parts.query}"

    return norm(left) == norm(right)


def _squeeze(value: str) -> str:
    return " ".join(value.split())
