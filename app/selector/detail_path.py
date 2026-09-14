"""클릭해서 알아낸 요청을 상세 설정으로 바꾼다.

`app/crawler/click_probe.py` 가 항목을 눌러 무엇이 나갔는지까지 알려 준다. 여기서는 그중
데이터로 보이는 요청 하나를 고르고, 다음 실행부터 브라우저 없이 부를 수 있는 모양
(`ApiDetailConfig`)으로 옮긴다.

## 공고 번호가 먼저다

요청을 고르는 기준이 "공고 번호가 들어 있는가" 다. 페이지 하나가 내는 요청 중 대부분은 공통
설정·배너·추천 목록이고, 그것들은 어느 공고를 눌러도 같다. 항목에서 읽은 번호가 주소나 본문에
들어 있는 요청만이 그 공고를 지목한 요청이다.

그래서 항목에서 번호가 될 만한 값을 먼저 모은다 — 링크의 마지막 경로 조각, 링크의 쿼리 값,
항목의 `data-` 속성. 삼성은 `a[data-value="22,878"]` 이고 쉼표를 뺀 `22878` 이 실제 번호다
(`.claude/site-recipes/www-samsungcareers-com.md`).

## 만드는 것은 제안이다

필드 경로는 응답에 실제로 있는 자리 중에서 이름이 비슷한 것을 고른 것이다. 맞는지는 운영자가
본다. **응답에 없는 경로를 지어내지 않는다** — 못 찾은 필드는 비운 채로 두고 이름을 적는다
(`.claude/rules/llm.md` 의 "제안자이지 권위가 아니다" 와 같은 자리다).

만든 설정은 돌려주기 전에 `validate_api_config()` 를 지난다. 검증을 통과하지 못하는 설정을
제안으로 내놓으면 운영자가 저장 버튼을 누를 때 처음 알게 된다.

## 채택 전에 `httpx` 로 다시 불러 본다

브라우저는 쿠키와 여러 헤더를 이미 들고 있다. 그 상태에서 나간 요청이 공용 fetch 클라이언트
로도 되는지는 별개 문제이고, **확인하지 않고 저장하면 등록만 성공하고 이후 실행이 전부
실패한다.** `confirm_api_path()` 가 같은 요청을 공용 클라이언트로 한 번 부르고, 브라우저가
받은 응답에서 읽은 제목·본문과 값이 같을 때만 채택한다.

값으로 견주는 이유는 둘 다 피하기 위해서다. 응답 전체를 견주면 조회수 한 자리가 달라도
거절하고, 상태 코드만 보면 로그인 페이지가 200 으로 오는 사이트를 통과시킨다.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from bs4.element import Tag

from app.crawler.api_source import build_detail, fetch_detail
from app.crawler.fetcher import FetchError, FetchPolicy
from app.crawler.parser import CrawlDataError
from app.crawler.playwright import ObservedRequest
from app.selector.api_schema import (
    ApiConfig,
    ApiConfigError,
    validate_api_config,
)
from app.selector.schema import DETAIL_FIELDS

logger = logging.getLogger(__name__)

# 상세를 무엇으로 가져오는가. `document` 는 주소를 그대로 여는 것이고, `api` 는 알아낸 요청을
# 다시 부르는 것이다
DOCUMENT = "document"
API = "api"

# 공고 번호로 볼 값의 길이. 너무 짧으면 아무 요청에나 우연히 들어 있다
MIN_ID_LENGTH = 3
MAX_ID_LENGTH = 64

# 응답에서 필드 자리를 찾을 때 훑는 깊이. 이보다 깊은 자리는 사람이 적는 것이 낫다
MAX_FIELD_DEPTH = 6
# 본문으로 볼 값의 최소 길이. 한 글자짜리 코드값을 본문으로 고르지 않는다
MIN_BODY_LENGTH = 30

# 필드마다 이름에 들어갈 만한 조각. 앞에 있는 것이 먼저다.
# 여섯 사이트의 응답 키에서 뽑았다 — `jobNoticeName`(LG), `rtNm`(한화), `title`(삼성),
# `recuNoticeNm`(현대), `detailContext`(LG), `taskKr`(삼성), `privJdDtl`(현대)
_FIELD_HINTS: dict[str, tuple[str, ...]] = {
    "title": ("noticename", "noticenm", "title", "subject", "jobname", "rtnm", "nm"),
    "body": (
        "detailcontext",
        "jddtl",
        "taskkr",
        "dtljob",
        "context",
        "content",
        "description",
        "intro",
        "task",
        "duty",
        "body",
    ),
    "qualifications": ("qlfct", "qualif", "requireditem", "require", "favor", "prefer", "exmqlf"),
    # 값은 사이트 API 의 키에 들어 있을 법한 글자다. 우리 칸 이름이 아니다
    "recruitment_end_at": (
        "enddate",
        "enddttm",
        "endd",
        "closedate",
        "deadline",
        "acptend",
        "endt",
    ),
    "department": ("department", "dept", "organiz", "orgn", "team", "part"),
    "company_name": ("cmpname", "companyname", "company", "cmpnm", "corpnm", "affiliate"),
}

# 링크에서 번호를 읽을 때 쓰는 표시. `IdSource.kind` 값이다
FROM_LINK = "link"
FROM_QUERY = "query"
FROM_ATTRIBUTE = "attribute"

_DIGITS = re.compile(r"\d")


@dataclass(frozen=True)
class IdSource:
    """항목에서 공고 번호를 어떻게 얻는지.

    `value` 는 이 항목에서 실제로 읽은 값이다. 설정에 저장되는 것은 읽는 법(`kind`, `detail`)
    이고, 값 자체는 항목마다 달라진다.
    """

    kind: str
    detail: str
    value: str
    digits: bool = False

    def describe(self) -> str:
        """사람이 읽을 한 줄. 다음에 이 사이트가 깨졌을 때 어디를 볼지가 여기 있다."""
        where = {
            FROM_LINK: f"링크의 경로 조각 `{self.detail}`",
            FROM_QUERY: f"링크의 쿼리 `{self.detail}`",
            FROM_ATTRIBUTE: f"항목의 `{self.detail}` 속성",
        }.get(self.kind, self.detail)
        tail = " (숫자만 남김)" if self.digits else ""
        return f"{where} 에서 공고 번호 {self.value}{tail}"


@dataclass(frozen=True)
class DetailPath:
    """상세로 가는 길 하나. `reason` 이 비어 있을 때만 쓸 수 있다."""

    kind: str = ""
    url: str = ""
    api: ApiConfig | None = None
    id_source: IdSource | None = None
    notes: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.reason


def id_candidates(node: Tag, link: str = "") -> list[IdSource]:
    """항목에서 공고 번호가 될 만한 값을 모은다. 그럴듯한 순서로 돌려준다.

    링크가 먼저다. 링크에 번호가 있으면 그것이 그 사이트가 공고를 지목하는 방식이고, 속성은
    링크가 없는 사이트(삼성·현대)에서 쓴다.
    """
    found: list[IdSource] = []
    seen: set[str] = set()

    def add(source: IdSource) -> None:
        if not _usable_id(source.value) or source.value in seen:
            return
        seen.add(source.value)
        found.append(source)

    if link:
        parts = urlsplit(link)
        path = parts.path.rstrip("/")
        if "/" in path:
            add(IdSource(kind=FROM_LINK, detail="마지막", value=path.rsplit("/", 1)[-1]))
        for name, value in parse_qsl(parts.query, keep_blank_values=False):
            add(IdSource(kind=FROM_QUERY, detail=name, value=value.strip()))

    for element in [node, *node.find_all(True, limit=40)]:
        if not isinstance(element, Tag):
            continue
        for name, raw in element.attrs.items():
            if not str(name).startswith("data-"):
                continue
            value = " ".join(raw).strip() if isinstance(raw, list) else str(raw or "").strip()
            add(IdSource(kind=FROM_ATTRIBUTE, detail=str(name), value=value))
            digits = "".join(char for char in value if char.isdigit())
            if digits != value:
                # 삼성은 `data-value="22,878"` 이고 주소에 들어가는 것은 `22878` 이다
                add(IdSource(kind=FROM_ATTRIBUTE, detail=str(name), value=digits, digits=True))

    return found


def pick_detail_request(
    requests: tuple[ObservedRequest, ...] | list[ObservedRequest],
    candidates: list[IdSource],
) -> tuple[ObservedRequest, IdSource] | None:
    """클릭 뒤 나간 요청 중 그 공고를 지목한 것 하나를 고른다.

    고르는 기준은 셋이다 — 200 으로 답했고, 공고 번호가 주소나 본문에 들어 있고, 응답이 값이
    있는 것. JSON 이 HTML 보다 먼저다.
    """
    scored: list[tuple[tuple[int, int, int], ObservedRequest, IdSource]] = []
    for order, request in enumerate(requests):
        if request.status != 200 or not request.body.strip():
            continue
        for rank, source in enumerate(candidates):
            if not request.contains(source.value):
                continue
            scored.append(((0 if request.is_json else 1, rank, order), request, source))
            break

    if not scored:
        return None
    scored.sort(key=lambda entry: entry[0])
    _, request, source = scored[0]
    logger.info("상세 요청 후보 채택 url=%s id=%s", request.url, source.value)
    return request, source


def propose_detail_config(request: ObservedRequest, source: IdSource) -> DetailPath:
    """고른 요청을 `ApiDetailConfig` 로 옮긴다. 공고 번호 자리는 `{id}` 가 된다."""
    if not request.is_json:
        return DetailPath(
            reason=(
                f"응답이 JSON 이 아니다(content-type={request.content_type or '없음'}). "
                "상세 설정은 JSON 응답만 읽는다. HTML 로 오는 상세는 문서 경로로 둔다"
            )
        )

    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError as exc:
        return DetailPath(reason=f"응답을 JSON 으로 읽지 못했다: {exc}")

    body, note = _request_body(request, source.value)
    if note:
        return DetailPath(reason=note)

    url = request.url.replace(source.value, "{id}")
    fields, missing = _field_paths(payload)
    if "title" not in fields or "body" not in fields:
        return DetailPath(
            reason=(
                "응답에서 제목이나 본문으로 볼 자리를 찾지 못했다. 경로를 손으로 적는다: "
                f"{request.url}"
            )
        )

    data = {
        "detail": {
            "url": url,
            "method": request.method,
            "body": body,
            "fields": fields,
        }
    }
    try:
        config = validate_api_config(data)
    except ApiConfigError as exc:
        # 형식을 통과하지 못하는 설정은 제안이 아니다. 무엇이 걸렸는지 그대로 옮긴다
        return DetailPath(reason=f"만든 설정이 형식 검증에 걸렸다({exc.reason}): {exc}")

    notes = [f"{name}: {_describe(path)}" for name, path in fields.items()]
    return DetailPath(
        kind=API,
        url=url,
        api=config,
        id_source=source,
        notes=(source.describe(), *notes),
        missing=tuple(missing),
    )


def document_path(url: str, note: str) -> DetailPath:
    """상세가 HTML 문서인 경우. 주소를 그대로 열면 되므로 설정이 필요 없다."""
    return DetailPath(kind=DOCUMENT, url=url, notes=(note,))


def _request_body(request: ObservedRequest, item_id: str) -> tuple[dict[str, Any], str]:
    """보낸 본문을 설정에 담을 모양으로. 담을 수 없으면 사유를 함께 돌려준다."""
    raw = request.request_body.strip()
    if request.method != "POST" or not raw:
        return {}, ""

    if raw.startswith(("{", "[")):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {}, f"보낸 본문을 JSON 으로 읽지 못했다: {exc}"
        if not isinstance(parsed, Mapping):
            return {}, f"보낸 본문이 객체가 아니다: {type(parsed).__name__}"
        return {key: _with_placeholder(value, item_id) for key, value in parsed.items()}, ""

    if "=" in raw:
        # 폼 본문이다. 상세 설정에는 폼으로 보낼 자리가 없어 그대로 담을 수 없다
        return {}, (
            "이 상세 요청은 폼 본문으로 나간다. 상세 설정은 JSON 본문만 보낼 수 있어 그대로 "
            f"담을 수 없다: {raw[:120]}"
        )
    return {}, f"보낸 본문의 형식을 알 수 없다: {raw[:120]}"


def _with_placeholder(value: Any, item_id: str) -> Any:
    """본문 안의 공고 번호를 `{id}` 로 바꾼다. 숫자로 보낸 자리도 찾는다."""
    if isinstance(value, str):
        return value.replace(item_id, "{id}")
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float) and str(value) == item_id:
        return "{id}"
    if isinstance(value, Mapping):
        return {key: _with_placeholder(inner, item_id) for key, inner in value.items()}
    if isinstance(value, list):
        return [_with_placeholder(inner, item_id) for inner in value]
    return value


def _field_paths(payload: Any) -> tuple[dict[str, Any], list[str]]:
    """응답에서 상세 필드가 있을 만한 자리를 고른다. 못 찾은 필드는 이름만 돌려준다.

    응답에 실제로 있는 경로만 고른다. 이름이 비슷한 자리가 없으면 비워 둔다 — 아무 자리나
    골라 두면 공고마다 엉뚱한 값이 들어가고, 그것은 빈 값보다 고치기 어렵다.
    """
    found = list(_walk(payload))
    fields: dict[str, Any] = {}
    missing: list[str] = []

    for name in DETAIL_FIELDS:
        hints = _FIELD_HINTS.get(name, ())
        best: tuple[int, int, int, str] | None = None
        for path, value in found:
            key = path.rsplit(".", 1)[-1].lower()
            if name == "body" and len(value) < MIN_BODY_LENGTH:
                continue
            for rank, hint in enumerate(hints):
                if hint not in key:
                    continue
                # 같은 이름이 한국어·영어로 둘 다 있는 사이트가 있다. 삼성은 `qlfctKr` 과
                # `qlfctEn` 이 나란히 오고, 소비 측에 나가는 것은 한국어 쪽이다
                score = (rank, 1 if key.endswith("en") else 0, path.count("."), path)
                if best is None or score < best:
                    best = score
                break
        if best is None:
            missing.append(name)
            continue
        fields[name] = best[3]

    return fields, missing


def _walk(payload: Any, prefix: str = "", depth: int = 0) -> Iterator[tuple[str, str]]:
    """응답을 훑어 (경로, 값) 을 낸다. 배열은 첫 칸을 보고 `*` 경로를 만든다.

    `*` 는 배열의 칸마다 하나씩 읽으라는 표시다. 한 칸만 읽으면 모집 부문이 여럿인 사이트에서
    본문이 반쪽이 된다 (`app/crawler/api_source.py`).
    """
    if depth > MAX_FIELD_DEPTH:
        return
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            yield from _walk(value, path, depth + 1)
        return
    if isinstance(payload, list):
        if payload and isinstance(payload[0], Mapping) and prefix:
            yield from _walk(payload[0], f"{prefix}.*", depth + 1)
        return
    if isinstance(payload, str) and payload.strip() and prefix:
        yield prefix, payload
    elif isinstance(payload, int) and not isinstance(payload, bool) and prefix:
        yield prefix, str(payload)


def _usable_id(value: str) -> bool:
    """공고 번호로 볼 만한 값인가. 짧거나 숫자가 없는 값은 아무 요청에나 걸린다."""
    text = value.strip()
    if not MIN_ID_LENGTH <= len(text) <= MAX_ID_LENGTH:
        return False
    return bool(_DIGITS.search(text))


def _describe(path: Any) -> str:
    return path if isinstance(path, str) else " + ".join(path)


@dataclass(frozen=True)
class Confirmation:
    """알아낸 경로를 `httpx` 로 다시 불러 본 결과.

    `adopted` 가 참일 때만 그 경로를 저장한다. **확인 없이 저장하지 않는다** — 브라우저에서만
    되는 요청을 저장하면 등록은 성공한 것처럼 보이고 이후 실행이 전부 실패한다.
    """

    adopted: bool
    reason: str = ""
    title: str = ""
    body_length: int = 0


async def confirm_api_path(
    client: FetchPolicy, path: DetailPath, request: ObservedRequest
) -> Confirmation:
    """상세 API 제안을 공용 fetch 클라이언트로 다시 불러 같은 응답이 오는지 본다.

    비교는 값으로 한다. 응답 전체를 견주면 조회수나 시각 한 자리가 달라도 거절하게 되고,
    상태 코드만 보면 로그인 페이지가 200 으로 오는 사이트를 통과시킨다. 브라우저가 받은 응답
    에서 읽은 제목·본문과 다시 부른 응답에서 읽은 제목·본문이 같아야 채택이다.
    """
    if path.api is None or path.api.detail is None or path.id_source is None:
        return Confirmation(adopted=False, reason="상세 설정이 없다. 확인할 것이 없다")

    config = path.api.detail
    item_id = path.id_source.value
    try:
        expected = build_detail(json.loads(request.body), config)
    except (json.JSONDecodeError, CrawlDataError) as exc:
        return Confirmation(
            adopted=False, reason=f"브라우저가 받은 응답에서 값을 읽지 못했다: {exc}"
        )

    try:
        actual = await fetch_detail(client, config, item_id)
    except FetchError as exc:
        return Confirmation(
            adopted=False,
            reason=(
                f"공용 fetch 클라이언트로 부르지 못했다: {exc}. "
                "브라우저에서만 되는 요청이라면 헤더나 쿠키가 필요하다"
            ),
        )
    except CrawlDataError as exc:
        return Confirmation(
            adopted=False,
            reason=(
                f"다시 부른 응답이 브라우저가 받은 것과 다르다: {exc}. "
                "헤더나 쿠키가 필요한 요청일 수 있다"
            ),
        )

    for name in ("title", "body"):
        if _same(expected.fields.get(name, ""), actual.fields.get(name, "")):
            continue
        return Confirmation(
            adopted=False,
            reason=(
                f"다시 부른 응답의 `{name}` 이 브라우저가 받은 것과 다르다. "
                f"브라우저 {len(expected.fields.get(name, ''))}자, "
                f"다시 부른 것 {len(actual.fields.get(name, ''))}자. "
                "헤더나 쿠키가 필요한 요청일 수 있다"
            ),
        )

    logger.info("상세 경로 채택 url=%s id=%s", config.url, item_id)
    return Confirmation(
        adopted=True,
        title=actual.fields.get("title", ""),
        body_length=len(actual.fields.get("body", "")),
    )


async def confirm_document_path(client: FetchPolicy, url: str, marker: str) -> Confirmation:
    """상세가 HTML 문서인 경우의 확인. 브라우저에서 본 제목이 정적 응답에도 있어야 한다.

    같으면 상세를 `static` 으로 둘 수 있다는 뜻이다. 없으면 그 페이지가 JS 로 그려지는
    것이므로 채택하지 않고, 상세를 렌더로 둘지는 운영자가 정한다
    (`.claude/rules/crawling.md` 의 "정적이 먼저, 렌더는 사이트별 승격").
    """
    if not marker.strip():
        return Confirmation(adopted=False, reason="대조할 제목이 없다. 확인할 것이 없다")
    try:
        result = await client.fetch(url)
    except FetchError as exc:
        return Confirmation(adopted=False, reason=f"공용 fetch 클라이언트로 부르지 못했다: {exc}")

    if _squeeze(marker) not in _squeeze(result.text):
        return Confirmation(
            adopted=False,
            reason=(
                f"정적으로 받은 문서에 제목 `{marker}` 이 없다. 브라우저에서만 그려지는 "
                "페이지라 상세를 렌더로 둬야 한다"
            ),
        )
    return Confirmation(adopted=True, title=marker, body_length=len(result.text))


def _same(expected: str, actual: str) -> bool:
    """두 값이 같은가. 공백과 개행 차이는 같은 것으로 본다 — 값이 지저분한 것은 정규화가
    다루는 문제이지 경로가 다르다는 뜻이 아니다 (`CLAUDE.md`)."""
    return _squeeze(expected) == _squeeze(actual)


def _squeeze(value: str) -> str:
    return " ".join(value.split())
