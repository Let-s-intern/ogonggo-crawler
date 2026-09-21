"""렌더 중 관찰한 응답에서 목록 API 를 찾는다.

`app/selector/detail_path.py` 가 클릭 뒤 나간 요청에서 **상세** 경로를 찾는다면, 여기서는
목록을 그리려고 페이지가 스스로 낸 요청에서 **목록** 경로를 찾는다. 하는 일의 모양이 같다 —
관찰한 요청을 설정으로 옮기고, 공용 fetch 클라이언트로 다시 불러 같은 것이 오는지 본 뒤에만
채택한다.

목록이 API 로 오는 사이트가 렌더로 남으면 실행마다 브라우저가 하나씩 뜬다. 인스턴스 하나가
150~300MB 이고 그것이 워크플로우가 시간 제한을 넘기는 주된 이유라, 목록 하나를 JSON 으로
받을 수 있다는 사실은 그 사이트의 실행 비용을 통째로 바꾼다 (`.claude/rules/crawling.md`).

## 어느 응답이 목록인가

**렌더된 항목의 제목이 응답 안에 있는가** 로 고른다. 배열 길이만 보면 페이지 하나가 내는
여러 응답 중 우연히 길이가 맞는 것(카카오 `jobTypeCountDtoList`, 우아한형제들
`job-groups/statistics`)이 같이 걸린다. 화면에 그려진 제목이 그 응답 안에 있다는 것은 그
응답으로 그 목록이 그려졌다는 뜻이고, 그것이 우연히 겹치는 일은 없다.

제목이 맞은 항목과 렌더된 항목을 짝지어 놓으면 나머지가 따라온다.

| 설정 | 어디서 나오는가 |
|---|---|
| `items_path` | 제목이 들어 있던 배열의 경로 |
| `fields.title` | 제목이 들어 있던 키의 경로 |
| `fields.date`·`fields.company_name` | 렌더된 항목의 날짜·회사명과 값이 같은 키 |
| `id_field` | 그 값이 렌더된 항목의 링크 안에 들어 있는 키 |
| `link_template` | 그 링크에서 id 자리를 `{id}` 로 바꾼 것 |

`link_template` 은 사이트가 그 항목에 실제로 걸어 둔 주소다. 쿼리를 떼거나 붙이지 않는다 —
`?category=...` 가 있어야 열리는 사이트인지 아닌지를 여기서 알 방법이 없고, 추측해서 떼면
공고마다 열리지 않는 주소가 `raw_jobs.source_url` 에 남는다.

찾지 못한 필드는 비운다. 이름이 비슷하다는 이유로 아무 키나 고르지 않는다
(`.claude/rules/llm.md` 의 "제안자이지 권위가 아니다").

## 브라우저에서만 되던 요청을 그대로 옮긴다

관찰한 요청에는 사이트가 요구하는 기능성 헤더가 붙어 있다. 그것을 떼고 다시 부르면 응답이
달라지거나 아예 오지 않는다 — HD현대 목록 API 는 `x-user-role` 이 없으면 500 이다. 관찰이
적어 둔 헤더(`ObservedRequest.request_headers`)를 확인에도 쓰고 설정에도 담는다.

## 잘린 응답은 다시 받아서 본다

관찰은 응답 본문을 `OBSERVED_BODY_LIMIT` 까지만 들고 있다. 공고를 수백 건씩 본문까지 담아
주는 목록 API 는 그 상한을 넘겨 JSON 으로 읽히지 않고, 그러면 "목록 API 가 없는 사이트" 로
판정된다 — HD현대 목록 응답이 1.4MB 다. `restore_truncated()` 가 그런 응답만 공용 fetch
클라이언트로 한 번 다시 받아 채운다. 어차피 확인 단계가 같은 주소를 부르므로 새로 생기는
요청 경로가 아니고, 여기서 실패하면 그 사유가 그대로 판정 사유가 된다.

## 쪽 넘김은 제안하지 않는다

관찰한 요청 하나에는 그 쪽만 들어 있다. 어느 파라미터가 쪽 번호인지, 마지막 쪽을 무엇으로
아는지는 응답 하나로는 알 수 없고, 틀리게 적으면 상한까지 사이트를 때린다. 첫 쪽만 가져오는
설정으로 저장하고, 쪽 넘김이 필요하면 운영자가 적는다 (`app/selector/api_schema.py`).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import parse_qsl

from app.crawler.api_source import fetch_list
from app.crawler.fetcher import FetchError, FetchPolicy
from app.crawler.parser import CrawlDataError, ListItem
from app.crawler.playwright import ObservedRequest
from app.selector.api_schema import (
    FORM_BODY,
    ID_PLACEHOLDER,
    JSON_BODY,
    ApiConfig,
    ApiConfigError,
    ApiListConfig,
    validate_api_config,
)

logger = logging.getLogger(__name__)

# 목록으로 볼 배열의 최소 길이. 공고가 한 건인 목록도 있지만, 한 건짜리 배열은 페이지가 내는
# 온갖 설정 응답에도 널려 있어 이것만으로는 목록이라고 말할 수 없다
MIN_ENTRIES = 2

# 제목이 맞아야 하는 최소 항목 수. 하나만 맞는 것은 우연일 수 있다
MIN_TITLE_HITS = 2

# 응답을 훑는 깊이. 이보다 깊은 자리는 사람이 적는 것이 낫다 (`detail_path.MAX_FIELD_DEPTH`)
MAX_DEPTH = 6

# id 로 볼 값의 길이. 짧은 값은 링크 아무 자리에나 우연히 들어 있다
MIN_ID_LENGTH = 3
MAX_ID_LENGTH = 64

# 사이트가 요구하는 기능성 헤더. 목록 API 를 브라우저 없이 부를 때 이것 하나로 갈리는 곳이
# 있다. User-Agent 는 담지 않는다 — 이름은 공용 fetch 클라이언트가 정한다
REFERER = "referer"

# 잘린 응답을 다시 받아 보는 횟수의 상한. 등록 한 번이 사이트를 여러 번 때리지 않게 한다
MAX_RESTORES = 3


@dataclass(frozen=True)
class ListPath:
    """목록으로 가는 길 하나. `reason` 이 비어 있을 때만 쓸 수 있다."""

    api: ApiConfig | None = None
    url: str = ""
    items_path: str = ""
    count: int = 0
    notes: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.reason and self.api is not None

    def config(self) -> ApiListConfig:
        """저장될 목록 설정. `ok` 일 때만 부른다."""
        assert self.api is not None and self.api.list is not None
        return self.api.list

    def with_referer(self, list_url: str) -> ListPath:
        """`referer` 를 넣어 같은 설정을 다시 만든다. 그것 없이는 답하지 않는 API 가 있다."""
        if not self.ok or not list_url.strip():
            return self
        config = self.config()
        headers = {**config.headers, REFERER: list_url}
        return replace(
            self,
            api=ApiConfig(list=config.model_copy(update={"headers": headers})),
            notes=(*self.notes, f"{REFERER}: {list_url}"),
        )


@dataclass(frozen=True)
class ListConfirmation:
    """알아낸 목록 API 를 `httpx` 로 다시 불러 본 결과.

    `adopted` 가 참일 때만 저장한다. 브라우저에서만 되는 요청을 저장하면 등록만 성공하고
    이후 실행이 전부 실패한다 (`app/selector/detail_path.py` 와 같은 자리다).
    """

    adopted: bool
    reason: str = ""
    matched: int = 0
    count: int = 0


def propose_list_config(
    requests: Sequence[ObservedRequest],
    items: Sequence[ListItem],
    links: Sequence[str] = (),
    clicked_url: str = "",
) -> ListPath:
    """관찰한 요청 중 이 목록을 그린 것을 골라 `ApiListConfig` 로 옮긴다.

    고르는 기준은 렌더된 항목의 제목이다. 못 고르면 왜 못 골랐는지를 `reason` 에 적는다 —
    "목록 API 가 없는 사이트" 와 "찾았는데 담을 수 없었다" 는 다음 행동이 다르다.

    `links` 는 렌더된 페이지에 실제로 걸려 있던 주소들이다. `ListItem.link` 를 쓰지 않는
    이유는 항목 자체가 `a` 인 사이트가 있기 때문이다 — 카카오 목록은 `<a><li>...</li></a>`
    라서 항목 안에서 링크를 찾는 셀렉터로는 주소가 나오지 않는다. 페이지에 걸린 주소 쪽을
    보면 셀렉터가 무엇이든 같은 증거를 쓴다.

    `clicked_url` 은 첫 항목을 눌러 도착한 상세 주소다. 페이지에 링크가 하나도 없는 사이트에서
    id 를 찾는 마지막 증거로 쓴다 (`_id_from_click`).
    """
    titles = [_squeeze(item.title) for item in items if item.title.strip()]
    if len(titles) < MIN_TITLE_HITS:
        return ListPath(
            reason=(
                f"렌더된 항목에서 제목을 {len(titles)}건만 읽었다. "
                "어느 응답이 이 목록을 그렸는지 제목으로 짚을 수 없다"
            )
        )

    best: tuple[tuple[int, int, int], ObservedRequest, str, list[Mapping[str, Any]]] | None = None
    for order, request in enumerate(requests):
        if request.status != 200 or not request.is_json or not request.body.strip():
            continue
        try:
            payload = json.loads(request.body)
        except json.JSONDecodeError:
            continue
        for path, entries in _arrays(payload):
            hits = _title_hits(entries, titles)
            if hits < MIN_TITLE_HITS or hits * 2 < min(len(titles), len(entries)):
                continue
            score = (-hits, abs(len(entries) - len(items)), order)
            if best is None or score < best[0]:
                best = (score, request, path, entries)

    if best is None:
        return ListPath(
            reason=(
                f"렌더 중 나간 요청 {len(requests)}건 중 이 목록을 담은 JSON 응답이 없다. "
                "목록이 초기 HTML 에 이미 들어 있거나 API 로 오지 않는 사이트다"
            )
        )

    _, request, path, entries = best
    return _build(request, path, entries, items, _usable_links(links, items), clicked_url)


def _build(
    request: ObservedRequest,
    items_path: str,
    entries: Sequence[Mapping[str, Any]],
    items: Sequence[ListItem],
    links: Sequence[str],
    clicked_url: str = "",
) -> ListPath:
    """고른 배열을 설정으로 옮긴다. 제목과 링크가 있어야 목록이 된다."""
    matches = _pairs(entries, items)
    title_path = _common(path for path, _, _ in matches)
    if not title_path:
        return ListPath(reason="배열은 찾았는데 제목이 든 키가 항목마다 달랐다")

    pairs = [(entry, item) for _, entry, item in matches]
    id_field, link_template = _id_and_link([entry for entry, _ in pairs], links)
    if not id_field and clicked_url and items:
        id_field, link_template = _id_from_click(entries, pairs, items[0], clicked_url)
    if not id_field:
        return ListPath(
            reason=(
                "응답의 어느 값도 렌더된 페이지의 상세 주소 안에 없다. 공고를 지목할 id 를 "
                "찾지 못해 상세 주소를 만들 수 없다"
            ),
            url=request.url,
            items_path=items_path,
        )

    fields: dict[str, Any] = {"title": title_path}
    missing: list[str] = []
    for name in ("date", "company_name"):
        found = _value_path([(entry, getattr(item, name)) for entry, item in pairs])
        if found:
            fields[name] = found
        else:
            missing.append(name)

    body, body_format = _request_body(request)
    # 브라우저가 붙였던 기능성 헤더를 그대로 옮긴다. 이것 하나로 200 과 500 이 갈리는 API 가
    # 있어서, 떼고 저장하면 등록만 성공하고 이후 실행이 전부 실패한다
    headers = dict(request.request_headers)
    data = {
        "list": {
            "url": request.url,
            "method": request.method,
            "body": body,
            "body_format": body_format,
            "items_path": items_path,
            "fields": fields,
            "id_field": id_field,
            "link_template": link_template,
            "headers": headers,
        }
    }
    try:
        config = validate_api_config(data)
    except ApiConfigError as exc:
        return ListPath(reason=f"만든 설정이 형식 검증에 걸렸다({exc.reason}): {exc}")

    notes = [
        f"items_path: {items_path} ({len(entries)}건)",
        f"title: {title_path}",
        f"id_field: {id_field}",
        f"link_template: {link_template}",
        *(f"{name}: {fields[name]}" for name in ("date", "company_name") if name in fields),
        *([f"headers: {', '.join(sorted(headers))}"] if headers else []),
    ]
    logger.info("목록 API 후보 url=%s items_path=%s 항목=%d", request.url, items_path, len(entries))
    return ListPath(
        api=config,
        url=request.url,
        items_path=items_path,
        count=len(entries),
        notes=tuple(notes),
        missing=tuple(missing),
    )


async def restore_truncated(
    client: FetchPolicy, requests: Sequence[ObservedRequest]
) -> list[ObservedRequest]:
    """본문이 잘린 JSON 응답만 공용 fetch 클라이언트로 다시 받아 채운다.

    관찰은 응답을 `OBSERVED_BODY_LIMIT` 까지만 들고 있다. 공고를 본문까지 수백 건 담아 주는
    목록 API 는 그 상한을 넘겨 `json.loads` 가 실패하고, 그러면 제안 단계에서 "이 목록을 담은
    JSON 응답이 없다" 로 끝난다. 실제로는 있는데 다 못 읽은 것이다 (HD현대 1.4MB).

    **브라우저를 닫은 뒤에 부른다.** 렌더는 호스트 잠금을 잡고 있어서, 그 안에서 같은 호스트로
    정적 요청을 내면 자기 잠금을 기다리며 멈춘다 (`app/crawler/fetcher.py` 의 `guard`).

    다시 받지 못한 요청은 잘린 채로 둔다. 여기서 실패하는 요청은 확인 단계에서도 실패하므로,
    막는 자리를 앞당길 뿐 판정을 바꾸지 않는다.
    """
    restored: list[ObservedRequest] = []
    tried = 0
    for request in requests:
        if tried >= MAX_RESTORES or not _restorable(request):
            restored.append(request)
            continue
        tried += 1
        filled = await _refetch(client, request)
        restored.append(filled if filled is not None else request)
    return restored


def _restorable(request: ObservedRequest) -> bool:
    """다시 받아 볼 만한 요청인가. 잘린 JSON 응답 하나가 조건이다."""
    return (
        request.truncated
        and request.status == 200
        and request.is_json
        and request.method in ("GET", "POST")
    )


async def _refetch(client: FetchPolicy, request: ObservedRequest) -> ObservedRequest | None:
    """같은 요청을 한 번 더 보내 본문을 통째로 받는다. 못 받으면 None 이다."""
    body, body_format = _request_body(request)
    headers = dict(request.request_headers)
    try:
        result = await client.request(
            request.url,
            method=request.method,
            json_body=body if body and body_format == JSON_BODY else None,
            form_body=body if body and body_format == FORM_BODY else None,
            headers=headers or None,
        )
    except FetchError as exc:
        logger.info("잘린 응답을 다시 받지 못했다 url=%s: %s", request.url, exc)
        return None
    if not result.text.strip():
        return None
    logger.info("잘린 응답을 다시 받았다 url=%s %d자", request.url, len(result.text))
    return replace(request, body=result.text, truncated=False)


async def confirm_list_path(
    client: FetchPolicy, path: ListPath, items: Sequence[ListItem]
) -> ListConfirmation:
    """목록 API 제안을 공용 fetch 클라이언트로 다시 불러 같은 목록이 오는지 본다.

    견주는 것은 제목이다. 응답 전체를 견주면 조회수 한 자리에 거절하고, 상태 코드만 보면
    로그인 페이지가 200 으로 오는 사이트를 통과시킨다 (`app/selector/detail_path.py`).
    """
    if not path.ok:
        return ListConfirmation(adopted=False, reason="목록 설정이 없다. 확인할 것이 없다")

    expected = [_squeeze(item.title) for item in items if item.title.strip()]
    try:
        result = await fetch_list(client, path.config())
    except FetchError as exc:
        return ListConfirmation(
            adopted=False,
            reason=(
                f"공용 fetch 클라이언트로 부르지 못했다: {exc}. "
                "브라우저에서만 되는 요청이라면 헤더가 필요하다"
            ),
        )
    except CrawlDataError as exc:
        return ListConfirmation(
            adopted=False, reason=f"다시 부른 응답에서 항목을 읽지 못했다: {exc}"
        )

    got = {_squeeze(item.title) for item in result.items}
    matched = sum(1 for title in expected if title in got)
    if matched < MIN_TITLE_HITS or matched * 2 < min(len(expected), len(result.items)):
        return ListConfirmation(
            adopted=False,
            reason=(
                f"다시 부른 목록 {len(result.items)}건 중 브라우저가 그린 제목과 같은 것이 "
                f"{matched}건뿐이다. 헤더나 쿠키가 필요한 요청일 수 있다"
            ),
            matched=matched,
            count=len(result.items),
        )

    logger.info("목록 API 채택 url=%s 항목=%d 제목일치=%d", path.url, len(result.items), matched)
    return ListConfirmation(adopted=True, matched=matched, count=len(result.items))


def _arrays(payload: Any, prefix: str = "", depth: int = 0) -> Iterator[tuple[str, list[Any]]]:
    """응답 안의 (경로, 객체 배열) 을 낸다. 배열 안의 배열까지는 보지 않는다."""
    if depth > MAX_DEPTH:
        return
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, list):
                entries = [entry for entry in value if isinstance(entry, Mapping)]
                if len(entries) >= MIN_ENTRIES and len(entries) == len(value):
                    yield path, value
                continue
            yield from _arrays(value, path, depth + 1)


def _title_hits(entries: Sequence[Any], titles: Sequence[str]) -> int:
    """이 배열이 그 제목들을 몇 건이나 담고 있는가."""
    found = 0
    for title in titles:
        if any(title in _values(entry).values() for entry in entries):
            found += 1
    return found


def _pairs(
    entries: Sequence[Mapping[str, Any]], items: Sequence[ListItem]
) -> list[tuple[str, Mapping[str, Any], ListItem]]:
    """제목이 같은 (키 경로, 응답 항목, 렌더된 항목) 짝."""
    found: list[tuple[str, Mapping[str, Any], ListItem]] = []
    for item in items:
        title = _squeeze(item.title)
        if not title:
            continue
        for entry in entries:
            path = next((key for key, value in _values(entry).items() if value == title), "")
            if path:
                found.append((path, entry, item))
                break
    return found


def _id_and_link(entries: Sequence[Mapping[str, Any]], links: Sequence[str]) -> tuple[str, str]:
    """응답의 값 중 페이지에 걸린 주소 안에 들어 있는 것을 찾는다.

    찾은 키가 `id_field` 이고, 그 값을 `{id}` 로 바꾼 주소가 `link_template` 이다. 항목 하나만
    보고 정하지 않는다 — 값 하나가 우연히 어느 주소에 들어 있는 일은 흔하고, 그것을 id 로
    저장하면 공고마다 같은 주소가 나온다. 항목마다 **서로 다른** 주소가 걸려야 채택한다.
    """
    hits: dict[str, list[tuple[str, str]]] = {}
    for entry in entries:
        for key, value in _values(entry).items():
            if not _usable_id(value):
                continue
            found = next((link for link in links if _token_in(value, link)), "")
            if found:
                hits.setdefault(key, []).append((value, found))

    usable = {
        key: found
        for key, found in hits.items()
        # 항목마다 다른 주소여야 한다. 같은 주소가 두 번 나오면 그 값은 공고를 가르지 않는다
        if len(found) >= MIN_TITLE_HITS and len({link for _, link in found}) == len(found)
    }
    if not usable:
        return "", ""

    # 많이 맞은 키가 먼저다. 같으면 값이 긴 쪽 — 짧은 값일수록 우연히 걸린다
    best = max(usable, key=lambda key: (len(usable[key]), len(usable[key][0][0])))
    value, link = usable[best][0]
    return best, _token_pattern(value).sub(ID_PLACEHOLDER, link, count=1)


def _token_pattern(value: str) -> re.Pattern[str]:
    """주소 안에서 그 값이 한 칸을 통째로 차지하는 자리. 앞뒤가 글자·숫자면 다른 값의 일부다."""
    return re.compile(rf"(?<![0-9A-Za-z]){re.escape(value)}(?![0-9A-Za-z])")


def _token_in(value: str, link: str) -> bool:
    """값이 주소의 한 칸(경로 조각·쿼리 값 등)과 통째로 같은가.

    부분 문자열로 보면 우연히 겹친다. KT 실측(2026-09-17)에서 정렬 순서 `sortOrder` 의 `266` 이
    공고 주소 `/careers/266550` 앞자리에 들어 있어 id 로 뽑혔고, 주소 형식이 `/careers/{id}550` 이
    돼 실행마다 모든 공고가 같은 주소로 갔다.
    """
    return _token_pattern(value).search(link) is not None


def _id_from_click(
    entries: Sequence[Mapping[str, Any]],
    pairs: Sequence[tuple[Mapping[str, Any], ListItem]],
    clicked: ListItem,
    clicked_url: str,
) -> tuple[str, str]:
    """첫 항목을 눌러 도착한 주소 하나로 id 를 찾는다. 페이지에 링크가 없는 사이트의 마지막 길이다.

    롯데ON 실측(2026-09-17): 항목이 링크도 `data-` 속성도 없는 `div` 이고, 누르면 새 탭으로
    `/job_posting/UJFP0dBm` 이 열렸다. 목록 응답에는 `"addressKey": "UJFP0dBm"` 이 제목과 함께
    있었지만, 대조할 주소가 페이지에 없어 id 를 못 골랐다.

    주소가 하나뿐이라 우연히 겹치는 값을 막는 조건을 더 건다. 누른 항목과 제목이 같은 응답
    항목에서, 도착 주소의 한 칸과 통째로 같은 값이고, 그 키가 **응답의 모든 항목에 있고 값이 서로
    다를 때만** 채택한다. 공고를 가르지 못하는 값은 여기서 떨어진다.
    """
    entry = next((one for one, item in pairs if item.index == clicked.index), None)
    if entry is None:
        return "", ""
    flattened = [_values(one) for one in entries]
    candidates: list[tuple[str, str]] = []
    for key, value in _values(entry).items():
        if not _usable_id(value) or not _token_in(value, clicked_url):
            continue
        values = [one.get(key, "") for one in flattened]
        if all(values) and len(set(values)) == len(values):
            candidates.append((key, value))
    if not candidates:
        return "", ""
    key, value = max(candidates, key=lambda pair: len(pair[1]))
    return key, _token_pattern(value).sub(ID_PLACEHOLDER, clicked_url, count=1)


def _usable_links(links: Sequence[str], items: Sequence[ListItem]) -> list[str]:
    """id 를 찾을 주소 목록. 페이지에 걸린 주소가 먼저고, 없으면 항목이 들고 있던 링크다."""
    found = [link for link in links if link.strip()]
    found.extend(item.link for item in items if item.link.strip() and not item.detail_absent)
    seen: set[str] = set()
    unique: list[str] = []
    for link in found:
        if link not in seen:
            seen.add(link)
            unique.append(link)
    return unique


def _value_path(pairs: Sequence[tuple[Mapping[str, Any], str]]) -> str:
    """렌더된 값과 같은 값을 가진 키의 경로. 여러 항목에서 같은 키가 맞아야 한다."""
    counts: dict[str, int] = {}
    wanted = 0
    for entry, raw in pairs:
        text = _squeeze(raw)
        if not text:
            continue
        wanted += 1
        for key, value in _values(entry).items():
            if value == text:
                counts[key] = counts.get(key, 0) + 1
    if not counts or wanted < MIN_TITLE_HITS:
        return ""
    best = max(counts, key=lambda key: counts[key])
    return best if counts[best] >= min(wanted, MIN_TITLE_HITS) else ""


def _request_body(request: ObservedRequest) -> tuple[dict[str, Any], str]:
    """보낸 본문을 설정에 담을 모양으로. GET 이면 담을 것이 없다."""
    raw = request.request_body.strip()
    if request.method != "POST" or not raw:
        return {}, JSON_BODY
    if raw.startswith(("{", "[")):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}, JSON_BODY
        return (dict(parsed) if isinstance(parsed, Mapping) else {}), JSON_BODY
    if "=" in raw:
        return dict(parse_qsl(raw, keep_blank_values=True)), FORM_BODY
    return {}, JSON_BODY


def _values(entry: Any, prefix: str = "", depth: int = 0) -> dict[str, str]:
    """항목 하나를 (경로 -> 문자열 값) 으로 편다. 값 비교는 전부 이 위에서 한다."""
    found: dict[str, str] = {}
    if depth > MAX_DEPTH or not isinstance(entry, Mapping):
        return found
    for key, value in entry.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, str):
            text = _squeeze(value)
            if text:
                found[path] = text
        elif isinstance(value, bool):
            continue
        elif isinstance(value, int | float):
            found[path] = str(value)
        elif isinstance(value, Mapping):
            found.update(_values(value, path, depth + 1))
    return found


def _common(paths: Iterator[str]) -> str:
    """가장 많이 나온 경로. 항목마다 다른 키에서 제목이 나오면 그 응답은 목록이 아니다."""
    counts: dict[str, int] = {}
    for path in paths:
        counts[path] = counts.get(path, 0) + 1
    if not counts:
        return ""
    best = max(counts, key=lambda key: counts[key])
    return best if counts[best] >= MIN_TITLE_HITS else ""


def _usable_id(value: str) -> bool:
    """공고 id 로 볼 만한 값인가. 짧은 값은 주소 아무 자리에나 우연히 들어 있다."""
    return MIN_ID_LENGTH <= len(value) <= MAX_ID_LENGTH


def _squeeze(value: str) -> str:
    return " ".join(value.split())
