"""JSON API 로 목록과 상세를 가져온다.

HTML 을 셀렉터로 읽는 자리(`app/crawler/parser.py`)와 하는 일이 같다. 다른 것은 입력이
문서가 아니라 JSON 이고, 어디를 읽을지 정하는 것이 CSS 셀렉터가 아니라 점 표기 경로라는
것뿐이다. 돌려주는 값은 같은 `ListParseResult` 와 `DetailParseResult` 라서, 러너와 그 뒤는
어느 경로로 왔는지 모른다.

요청은 공용 fetch 클라이언트의 `request()` 로 나간다. 이 모듈은 `httpx` 를 모른다
(`.claude/rules/crawling.md`).

## 실패는 셋으로 갈린다

| 무슨 일 | error_class | 고치는 법 |
|---|---|---|
| 타임아웃·5xx·연결 끊김 | `transport` | 재시도. 반복되면 사이트 상태 확인 |
| `items_path` 가 응답에 없거나 배열이 비었다 | `selector_miss` | 경로 재작성. 재시도 금지 |
| JSON 이 아니거나 배열이 아니거나 필드를 못 읽었다 | `parse` | 그 경로만 보정 |

**항목 0건은 실패다.** 200 이 오고 배열이 비어 있는 것은 "신규 공고 없음" 이 아니라 목록을
가져오지 못한 것이다. API 응답 모양이 바뀐 사이트와 공고가 없는 사이트를 같은 결과로 남기면
둘을 구분할 수 없다 (`CLAUDE.md`).

## 값은 손대지 않는다

LG 의 `detailContext` 처럼 HTML 조각이 그대로 들어 있는 필드가 있다. 여기서 텍스트로 펴지
않는다. 지저분한 값은 정규화 규칙이 다루는 문제이고, 수집 단계가 손대면 원본이 사라진다
(`CLAUDE.md`, `.claude/rules/data-safety.md`).

## 원문은 응답 전체를 편다

필드는 경로가 짚은 자리만 담는다. 응답에는 그 밖에도 공고 내용이 있다 — LG 의 근무지·필수
역량·전형 절차, 한화의 근무지·모집인원이 경로 밖에 있어 분류가 보지 못했다. 그래서 원문
(`DetailParseResult.source_text`)은 응답 전체를 `키: 값` 줄로 편 글자다
(`payload_source_text`). 필드와 달리 원문은 읽히는 글이라 HTML 을 펴고 쓸데없는 값을 덜어 낸다.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup
from bs4.element import Tag

from app.crawler.fetcher import FetchPolicy, FetchResult
from app.crawler.parser import (
    REQUIRED_DETAIL_FIELDS,
    DetailParseResult,
    FieldFailure,
    FieldParseError,
    ListItem,
    ListParseResult,
    SelectorMissError,
    field_text,
    select_nodes,
)
from app.normalize.engine import flatten_html
from app.selector.api_schema import (
    DIGITS_FILTER,
    FORM_BODY,
    ID_ATTRIBUTE_MARK,
    ID_PLACEHOLDER,
    LIST_FIELDS,
    ApiDetailConfig,
    ApiListConfig,
    FieldPath,
)
from app.selector.schema import DETAIL_FIELDS

logger = logging.getLogger(__name__)

# 배열 전체를 훑는 표시. `recList.*.detailContext` 는 모집 부문마다 하나씩이다
WILDCARD = ".*."

# `id_field` 에 쓰는 `{키}` 자리. 항목 안의 경로 이름만 받는다
ENTRY_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}")

# 항목 하나가 남으려면 있어야 하는 것. 제목이 없으면 공고를 알아볼 수 없고, id 가 없으면
# 상세로 갈 수도 주소를 만들 수도 없다
REQUIRED_LIST_VALUES: tuple[str, ...] = ("title", "id")

# 원문으로 펼 때 덜어 내는 값을 가르는 표시들 (`payload_source_text`)
_HANGUL = re.compile(r"[가-힣]")
# 링크와 파일 경로. 한글이 이만큼 섞인 줄은 문장 안에 주소가 든 것이라 남긴다
_LINK = re.compile(
    r"https?://|www\.|\.(?:png|jpe?g|gif|svg|webp|pdf|docx?|hwp|xlsx?|zip)\b", re.IGNORECASE
)
LINK_SENTENCE_HANGUL = 4
# 날짜·시각 모양. `2026.08.25`, `08/20`, `15:00`
_DATE_SHAPE = re.compile(r"\d{2,4}[.\-/년]\s?\d{1,2}|\d{1,2}:\d{2}")
# 붙여 쓴 날짜. 현대는 마감을 `20260830` 으로, 삼성은 `202609021700` 으로 준다
_COMPACT_DATE_FORMATS: dict[int, str] = {8: "%Y%m%d", 12: "%Y%m%d%H%M", 14: "%Y%m%d%H%M%S"}
# 네 자리 시각. `1705` 는 키 이름이 시각이라고 말할 때만 시각이다 — 아니면 번호와 구분이 안 된다
_CLOCK = re.compile(r"\d{4}")
_TIME_KEY = re.compile(r"(?:tm|time)$", re.IGNORECASE)
# 코드값을 이루는 글자. 다른 글자가 섞이면 코드가 아니다 — `[Finance]` 는 LG 의 직무 이름이다
_CODE_CHARS = re.compile(r"[A-Za-z0-9_.\-:/+#]+")
_ALNUM = re.compile(r"[A-Za-z0-9]")
_DIGIT = re.compile(r"\d")
_EMPTY_WORDS = frozenset({"null", "none", "true", "false", "undefined"})
_SPACES = re.compile(r"\s+")
# 이보다 짧은 줄은 겹쳐도 지우지 않는다. `서울` 이 두 번 나오는 것은 문장이 겹친 것이 아니다
DUPLICATE_MIN_LENGTH = 8


class _Missing:
    """경로가 안 잡혔다는 표시. `None` 은 "값이 null 이었다" 라서 자리가 다르다."""


MISSING = _Missing()


async def fetch_list(
    client: FetchPolicy,
    config: ApiListConfig,
    *,
    known: Callable[[str], bool] | None = None,
) -> ListParseResult:
    """목록 API 를 부르고 항목을 만든다. 쪽 넘김 설정이 있으면 끝까지 넘긴다.

    `known` 은 "이 주소는 이미 담은 공고인가" 다. 주면 아는 공고만 있는 쪽에서 멈춘다.
    """
    if config.pagination is None:
        result = await _send(client, config.url, config.method, dict(config.body), config)
        return _read_page(result, config)
    return await _fetch_pages(client, config, known=known)


async def _fetch_pages(
    client: FetchPolicy,
    config: ApiListConfig,
    *,
    known: Callable[[str], bool] | None = None,
) -> ListParseResult:
    """쪽을 넘겨 가며 전부 모은다. 한화 68건(20씩 4쪽)과 삼성 16건(2쪽)이 이 경로다.

    쪽 사이에도 호스트 딜레이가 그대로 걸린다. 요청이 전부 공용 fetch 클라이언트를 지나기
    때문이고, 그래서 여기서 따로 기다리지 않는다 (`.claude/rules/crawling.md`).

    멈추는 조건은 넷이다 — 사이트가 다음 쪽이 없다고 말했거나, 항목이 0건인 쪽이 나왔거나,
    **쪽이 통째로 아는 공고였거나**, `max_pages` 에 닿았거나. 마지막 것이 없으면 끝나지 않는
    `hasNext` 하나로 사이트를 영원히 때리게 된다.

    아는 공고로 멈추는 것은 목록이 새것부터 오기 때문이다. 한 쪽이 전부 아는 공고면 그 뒤는
    더 옛것이라 볼 이유가 없다. **한 건이 아니라 쪽 전체로 보는 것은 상단 고정 공고 때문이다**
    — 공지가 위에 박혀 있으면 첫 건은 늘 아는 공고이고, 그것으로 멈추면 새 공고를 영영 못 본다.

    첫 쪽이 0건인 것은 끝이 아니라 실패다. 목록을 못 읽은 실행과 공고가 없는 사이트를 같은
    결과로 남기지 않는다.
    """
    pages = config.pagination
    assert pages is not None  # 부르는 쪽이 확인했다

    matched = 0
    items: list[ListItem] = []
    failures: list[FieldFailure] = []
    number = pages.start

    for turn in range(pages.max_pages):
        body = dict(config.body)
        body[pages.param] = number
        result = await _send(client, config.url, config.method, body, config)
        try:
            page = _read_page(result, config)
        except SelectorMissError:
            if turn == 0:
                # 첫 쪽부터 0건이다. 쪽 넘김의 끝이 아니라 목록을 가져오지 못한 것이다
                raise
            logger.info("목록 %s쪽이 0건이라 여기서 멈춘다 url=%s", number, config.url)
            break

        for miss in page.failures:
            failures.append(replace(miss, index=miss.index + matched))
        for item in page.items:
            items.append(replace(item, index=len(items)))
        matched += page.matched

        if known is not None and page.items and all(known(item.link) for item in page.items):
            logger.info(
                "목록 %s쪽이 전부 아는 공고라 여기서 멈춘다 url=%s. 그 뒤는 더 옛것이다",
                number,
                config.url,
            )
            break

        if not _has_more(result, config, page_count=turn + 1):
            break
        number += 1
    else:
        logger.warning(
            "목록 쪽 넘김이 상한 %s에 닿아 멈춘다 url=%s. 그 뒤 공고는 이 실행에 없다",
            pages.max_pages,
            config.url,
        )

    return ListParseResult(matched=matched, items=items, failures=failures)


def _read_page(result: FetchResult, config: ApiListConfig) -> ListParseResult:
    """응답 하나를 항목으로 읽는다. JSON 인지 HTML 조각인지는 설정이 정한다."""
    if config.is_html:
        return build_html_items(result.text, config)
    return build_items(_as_json(result), config)


def _has_more(result: FetchResult, config: ApiListConfig, *, page_count: int) -> bool:
    """다음 쪽이 있는가. 판정하는 법은 사이트마다 다르다."""
    pages = config.pagination
    assert pages is not None

    if pages.has_next:
        # 한화는 `data.hasNext` 가 마지막 쪽에서 false 가 된다
        return _dig(_as_json(result), pages.has_next) is True
    if pages.total_pages_selector:
        # 삼성은 총 쪽 수를 응답 안에 넣어 준다
        total = _total_pages(result.text, pages.total_pages_selector, pages.total_pages_attribute)
        return total is not None and page_count < total
    # 판정할 것이 없으면 다음 쪽을 열어 보고 0건이면 멈춘다
    return True


def _total_pages(html: str, selector: str, attribute: str) -> int | None:
    """총 쪽 수. 못 읽으면 None 이고, 그때는 0건인 쪽이 나올 때까지 넘긴다."""
    soup = BeautifulSoup(html, "html.parser")
    nodes = select_nodes(soup, selector, "list.pagination.total_pages_selector")
    if not nodes:
        return None
    raw = nodes[0].get(attribute)
    value = " ".join(raw) if isinstance(raw, list) else str(raw or "")
    value = value.strip()
    return int(value) if value.isdigit() else None


async def fetch_detail(
    client: FetchPolicy, config: ApiDetailConfig, item_id: str
) -> DetailParseResult:
    """공고 하나의 상세 API 를 부르고 필드를 만든다."""
    url = config.url.replace(ID_PLACEHOLDER, item_id)
    body = _with_id(dict(config.body), item_id)
    result = await _send(client, url, config.method, body, config)
    return build_detail(_as_json(result), config)


def build_items(payload: Any, config: ApiListConfig) -> ListParseResult:
    """응답에서 항목을 뽑는다. 경로가 안 잡히거나 0건이면 `SelectorMissError` 다."""
    entries = _dig(payload, config.items_path)
    if isinstance(entries, _Missing):
        raise SelectorMissError(
            f"items_path `{config.items_path}` 가 응답에 없다. API 응답 모양이 바뀌었다"
        )
    if not isinstance(entries, list):
        # 경로는 잡혔는데 배열이 아니다. 경로 자체를 잘못 짚은 것이라 그 자리만 고치면 된다
        raise FieldParseError(
            f"items_path `{config.items_path}` 가 배열이 아니다: {type(entries).__name__}"
        )
    if not entries:
        raise SelectorMissError(
            f"items_path `{config.items_path}` 가 빈 배열이다. "
            "신규 0건인 정상 실행이 아니라 목록을 가져오지 못한 것이다"
        )

    items: list[ListItem] = []
    failures: list[FieldFailure] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            failures.append(
                FieldFailure(
                    index=index,
                    field="item",
                    message=f"항목이 객체가 아니다: {type(entry).__name__}",
                )
            )
            continue
        item, problems = _item(index, entry, config)
        if item is None:
            failures.extend(problems)
            continue
        items.append(item)

    if not items:
        unread = [name for name in REQUIRED_LIST_VALUES if any(f.field == name for f in failures)]
        raise FieldParseError(
            f"항목 {len(entries)}건을 잡았지만 어느 항목에서도 {', '.join(unread) or 'item'} 를 "
            f"읽지 못했다: {failures[0].message}"
        )
    return ListParseResult(matched=len(entries), items=items, failures=failures)


def build_html_items(html: str, config: ApiListConfig) -> ListParseResult:
    """HTML 조각으로 오는 목록에서 항목을 뽑는다.

    삼성 목록이 이 경로다. 요청은 API 처럼 POST 로 물어보는데 응답이 JSON 이 아니라 HTML 조각
    이라, 어디를 읽을지는 점 표기 경로가 아니라 CSS 셀렉터가 정한다.

    | 설정 | HTML 모드에서의 뜻 |
    |---|---|
    | `items_path` | 항목 하나를 잡는 셀렉터 |
    | `fields` | 항목 안에서 값을 잡는 셀렉터 |
    | `id_field` | `<셀렉터>@<속성>`. 셀렉터를 비우면 항목 노드 자신의 속성이다 |

    판정은 JSON 쪽과 같다. 항목 0건은 실패이고, 제목과 id 가 없는 항목은 남기지 않는다.
    """
    soup = BeautifulSoup(html, "html.parser")
    nodes = select_nodes(soup, config.items_path, "list.items_path")
    if not nodes:
        raise SelectorMissError(
            f"items_path `{config.items_path}` 가 0개 매칭됐다. 목록 조각의 구조가 바뀌었다"
        )

    items: list[ListItem] = []
    failures: list[FieldFailure] = []
    for index, node in enumerate(nodes):
        values = {
            name: _node_value(node, selector, f"list.{name}[{index}]")
            for name, selector in config.fields.items()
        }
        item_id = _html_id(node, config.id_field, index)

        problems: list[FieldFailure] = []
        if not values.get("title"):
            problems.append(
                FieldFailure(
                    index=index,
                    field="title",
                    message=(
                        f"`{_describe(config.fields.get('title', ''))}` 이 항목 안에서 "
                        "값을 찾지 못했다"
                    ),
                )
            )
        if not item_id:
            problems.append(
                FieldFailure(
                    index=index,
                    field="id",
                    message=f"id_field `{config.id_field}` 가 항목 안에서 값을 찾지 못했다",
                )
            )
        if problems:
            failures.extend(problems)
            continue

        items.append(
            ListItem(
                index=index,
                title=values.get("title", ""),
                link=config.link_template.replace(ID_PLACEHOLDER, item_id),
                date=values.get("date", ""),
                company=values.get("company", ""),
                detail_key=item_id,
                extra=_extra(values),
            )
        )

    if not items:
        unread = [name for name in REQUIRED_LIST_VALUES if any(f.field == name for f in failures)]
        raise FieldParseError(
            f"항목 {len(nodes)}건을 잡았지만 어느 항목에서도 {', '.join(unread) or 'item'} 를 "
            f"읽지 못했다: {failures[0].message}"
        )
    return ListParseResult(matched=len(nodes), items=items, failures=failures)


def _node_value(node: Tag, selector: FieldPath, name: str) -> str:
    """항목 안에서 값 하나. 셀렉터를 여럿 적으면 모아서 빈 줄로 잇는다."""
    selectors = [selector] if isinstance(selector, str) else list(selector)
    parts = [field_text(node, one, name) for one in selectors]
    return "\n\n".join(part for part in parts if part.strip())


def _html_id(node: Tag, spec: str, index: int) -> str:
    """`<셀렉터>@<속성>` 표기로 항목 id 를 읽는다.

    `|digits` 를 붙이면 숫자만 남긴다. 삼성 공고 번호가 `data-value="22,878"` 처럼 천 단위
    쉼표가 찍힌 채로 오는데, 그대로 상세 주소에 넣으면 `%2C` 로 인코딩돼 열리지 않는다.
    **숫자 표기에 기대는 자리다** — 사이트가 표기를 바꾸면 여기가 먼저 깨진다
    (`.claude/site-recipes/www-samsungcareers-com.md`).
    """
    wanted, _, _ = spec.partition(DIGITS_FILTER)
    selector, mark, attribute = wanted.partition(ID_ATTRIBUTE_MARK)
    if not mark or not attribute.strip():
        raise FieldParseError(f"HTML 목록의 id_field 는 `<셀렉터>@<속성>` 이어야 한다: {spec}")

    target = node
    if selector.strip():
        found = select_nodes(node, selector.strip(), f"list.id_field[{index}]")
        if not found:
            return ""
        target = found[0]

    raw = target.get(attribute.strip())
    value = " ".join(raw) if isinstance(raw, list) else str(raw or "")
    value = value.strip()
    if spec.endswith(DIGITS_FILTER):
        value = "".join(char for char in value if char.isdigit())
    return value


def build_detail(payload: Any, config: ApiDetailConfig) -> DetailParseResult:
    """응답에서 상세 필드를 뽑는다. 필수 필드를 못 읽으면 `FieldParseError` 다."""
    fields: dict[str, str] = {}
    missing: list[str] = []
    for name in DETAIL_FIELDS:
        path = config.fields.get(name)
        if not path:
            # 설정에 없는 필드다. 사이트에 그 항목이 없다는 뜻이지 실패가 아니다
            fields[name] = ""
            continue
        fields[name] = _value(payload, path)
        if not fields[name]:
            missing.append(name)

    unreadable = [name for name in REQUIRED_DETAIL_FIELDS if not fields[name]]
    if unreadable:
        raise FieldParseError(
            f"상세 응답에서 필수 필드를 읽지 못했다: {', '.join(unreadable)}. "
            "경로를 확인한다: "
            + ", ".join(_describe(config.fields.get(name, "")) for name in unreadable)
        )
    return DetailParseResult(
        fields=fields, missing=missing, source_text=payload_source_text(payload)
    )


def payload_source_text(payload: Any) -> str:
    """상세 응답 전체를 분류가 읽는 원문으로 편다. 값 하나가 `키: 값` 으로 시작하는 줄이 된다.

        rtNm: LIFEPLUS부문 'LIFEPLUS TV 마케팅 기획 및 운영
        rtAcptEndDttm: 2026.08.25 15:00
        rtExmQlf: ■ 직무 필요역량 및 자격요건
        ㆍ 학력: 국내외 4년제 대학교 학사 학위 이상

    키 이름을 붙이는 것은 짧은 값이 무엇인지 알게 하려고다. 날짜 두 줄 중 어느 것이 마감인지,
    `본사(서울 63빌딩)` 가 근무지인지는 키 이름 없이는 알 수 없다. 여러 줄인 값은 첫 줄에만
    붙인다. HTML 조각은 `flatten_html` 로 편다.

    덜어 내는 줄은 셋이다.

    | 줄 | 예 | 왜 |
    |---|---|---|
    | 링크·파일 경로 | `www.hanwhain.com`, `….png` | 공고 내용이 아니다 |
    | 코드값 | `N`, `SUCCESS`, `19463`, `G12002` | 화면에 안 보이는 값이다. 날짜는 남긴다 |
    | 같은 묶음 안에서 겹친 문장 | 삼성 직무마다 한글·영어 칸에 같은 자격요건 | 글자 수만 먹는다 |

    **겹친 문장은 같은 묶음 안에서만 지운다.** 묶음은 응답 배열의 한 칸이다 — 삼성은 직무
    열두 개가 열두 칸이고 칸마다 `- 2년 이상 유관경력 보유하신 분` 이 있다. 응답 전체에서 지우면
    둘째 직무부터 자격요건이 사라져, 직무별로 나눌 때 그 직무의 칸이 빈다.
    """
    lines: list[str] = []
    shown: dict[tuple[int, ...], set[str]] = {}
    for key, value, group in _leaves(payload):
        seen = shown.setdefault(group, set())
        labelled = False
        for raw in flatten_html(value).split("\n"):
            line = raw.strip()
            if not line or _is_link(line) or _is_code(key, line):
                continue
            same = _SPACES.sub(" ", line)
            if len(same) >= DUPLICATE_MIN_LENGTH:
                if same in seen:
                    continue
                seen.add(same)
            lines.append(line if labelled or not key else f"{key}: {line}")
            labelled = True
    return "\n".join(lines)


def _leaves(
    value: Any, key: str = "", group: tuple[int, ...] = ()
) -> Iterator[tuple[str, str, tuple[int, ...]]]:
    """응답 안의 값을 차례대로. 값마다 바로 위 키 이름과, 지나온 배열 칸 번호들이 붙는다."""
    if isinstance(value, Mapping):
        for name, inner in value.items():
            yield from _leaves(inner, str(name), group)
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            yield from _leaves(inner, key, (*group, index))
    elif isinstance(value, str | int | float) and not isinstance(value, bool):
        yield key, str(value), group


def _is_link(line: str) -> bool:
    return bool(_LINK.search(line)) and len(_HANGUL.findall(line)) < LINK_SENTENCE_HANGUL


def _is_code(key: str, line: str) -> bool:
    """사람이 읽는 글이 아니라 시스템이 쓰는 값인가. 띄어쓰기나 한글이 있으면 글이다."""
    if _HANGUL.search(line) or _SPACES.search(line):
        return False
    if _DATE_SHAPE.search(line) or _compact_date(line):
        return False
    if _CLOCK.fullmatch(line) and _TIME_KEY.search(key):
        return False
    if line.lower() in _EMPTY_WORDS or not _ALNUM.search(line):
        return True
    if not _CODE_CHARS.fullmatch(line):
        return False
    # `Finance` 같은 낱말은 남기고, 숫자가 섞였거나 전부 대문자인 것(`G12002`, `FORM`)만 코드다
    return bool(_DIGIT.search(line)) or line.upper() == line


def _compact_date(line: str) -> bool:
    fmt = _COMPACT_DATE_FORMATS.get(len(line))
    if fmt is None or not line.isdigit():
        return False
    try:
        datetime.strptime(line, fmt)
    except ValueError:
        return False
    return True


def _item(
    index: int, entry: Mapping[str, Any], config: ApiListConfig
) -> tuple[ListItem | None, list[FieldFailure]]:
    """항목 하나. 제목과 id 가 있어야 남는다."""
    values = {name: _value(entry, path) for name, path in config.fields.items()}
    item_id = _entry_id(entry, config.id_field)

    problems: list[FieldFailure] = []
    if not values.get("title"):
        problems.append(
            FieldFailure(
                index=index,
                field="title",
                message=(
                    f"`{_describe(config.fields.get('title', ''))}` 가 항목 안에서 값을 찾지 못했다"
                ),
            )
        )
    if not item_id:
        problems.append(
            FieldFailure(
                index=index,
                field="id",
                message=f"id_field `{config.id_field}` 가 항목 안에서 값을 찾지 못했다",
            )
        )
    if problems:
        return None, problems

    return (
        ListItem(
            index=index,
            title=values.get("title", ""),
            link=config.link_template.replace(ID_PLACEHOLDER, item_id),
            date=values.get("date", ""),
            company=values.get("company", ""),
            detail_key=item_id,
            extra=_extra(values),
        ),
        [],
    )


def _extra(values: Mapping[str, str]) -> dict[str, str]:
    """목록에서 읽은 값 중 상세 칸으로 갈 것들. 목록 자신의 세 값은 빼고 담는다.

    카카오 목록 API 는 직군·근무지·모집인원·주요 업무·전형 절차를 항목마다 담아 주는데 상세
    문서에는 그것들이 한 덩어리로만 있다. 여기서 싣지 않으면 그 값들은 수집 단계에서 사라지고,
    매핑하지 않은 값은 저장되지 않으므로 다시 얻을 길이 없다.

    빈 값은 담지 않는다. 상세가 비었을 때만 쓰이는 값이라, 빈 문자열을 담아 두면 "목록도 비었다"
    와 "목록이 그 값을 안 준다" 가 같은 모양이 된다.
    """
    return {
        name: value for name, value in values.items() if name not in LIST_FIELDS and value.strip()
    }


def _entry_id(entry: Mapping[str, Any], spec: str) -> str:
    """항목의 id. 키 하나이거나, `{키}` 자리를 항목 값으로 채운 템플릿이다.

    현대 상세는 `recuYy`·`recuType`·`recuCls` 세 값이 다 있어야 열린다. 한 값으로는 공고를
    지목할 수 없는 사이트라, id 자체가 여러 값을 이어 붙인 것이 된다.

        "id_field": "recuYy={recuYy}&recuType={recuType}&recuCls={recuCls}"

    한 자리라도 비면 빈 값이다. **반쯤 채워진 주소를 만들지 않는다** — 그것으로 요청하면 엉뚱한
    공고를 가져오거나 조용히 400 이 된다.
    """
    names = ENTRY_PLACEHOLDER.findall(spec)
    if not names:
        return _text(_dig(entry, spec))

    values: dict[str, str] = {}
    for name in names:
        value = _text(_dig(entry, name)).strip()
        if not value:
            return ""
        values[name] = value
    return ENTRY_PLACEHOLDER.sub(lambda match: values[match.group(1)], spec)


async def _send(
    client: FetchPolicy,
    url: str,
    method: str,
    body: dict[str, Any],
    config: ApiListConfig | ApiDetailConfig,
) -> FetchResult:
    """공용 클라이언트로 한 번 부른다. 본문 형식과 헤더는 설정이 정한다.

    GET 에 본문을 싣는 API 는 아직 만난 적이 없어 POST 일 때만 본문을 보낸다. 폼으로 보낼지는
    `body_format` 이 정한다 — 삼성은 폼이 아니면 500 을, 파라미터가 하나라도 빠지면
    `{"code":500}` 을 준다.
    """
    payload = body if method == "POST" else None
    as_form = getattr(config, "body_format", "") == FORM_BODY
    result = await client.request(
        url,
        method=method,
        json_body=None if as_form else payload,
        form_body=payload if as_form else None,
        headers=config.headers or None,
    )
    logger.info("api 응답 url=%s method=%s status=%s", url, method, result.status_code)
    return result


def _as_json(result: FetchResult) -> Any:
    """응답을 JSON 으로 읽는다. JSON 이 아니면 파싱 실패다."""
    try:
        return json.loads(result.text)
    except json.JSONDecodeError as exc:
        # 전송은 됐다. 200 인데 JSON 이 아니면 endpoint 를 잘못 짚었거나 사이트가 막은 것이고,
        # 어느 쪽이든 재시도로는 풀리지 않는다
        raise FieldParseError(
            f"응답이 JSON 이 아니다({exc}): {result.url} status={result.status_code}"
        ) from exc


def _with_id(value: Any, item_id: str) -> Any:
    """본문의 `{id}` 자리를 채운다. 값 전체가 자리표시자면 타입도 원래대로 돌린다.

    `{"jobNoticeId": "{id}"}` 는 `{"jobNoticeId": 1002029}` 로 나간다. LG 는 숫자를 받는데
    문자열로 보내면 응답이 비어 돌아온다. 문자열 안에 섞여 있는 경우는 문자열 그대로 채운다.
    """
    if isinstance(value, str):
        if value == ID_PLACEHOLDER:
            return int(item_id) if item_id.isdigit() else item_id
        return value.replace(ID_PLACEHOLDER, item_id)
    if isinstance(value, Mapping):
        return {key: _with_id(inner, item_id) for key, inner in value.items()}
    if isinstance(value, list):
        return [_with_id(inner, item_id) for inner in value]
    return value


def _value(payload: Any, path: FieldPath) -> str:
    """한 필드의 값. 여러 자리에서 모을 수 있고, 모은 것은 빈 줄로 잇는다.

    한 자리로는 필드가 반쪽이 되는 사이트가 있다.

    | 적는 법 | 뜻 | 어디서 필요한가 |
    |---|---|---|
    | `data.item.body` | 그 자리 하나 | 대부분 |
    | `data.recList.*.detailContext` | 배열 전체를 훑는다 | LG 모집 부문 6개, 삼성 직무 12개 |
    | `["a.b", "a.c"]` | 여러 자리를 모은다 | 현대의 주요 업무와 조직 소개 |

    첫 칸만 읽으면 나머지 부문의 본문이 그대로 사라진다. 수집 단계에서 사라진 값은 정규화를
    다시 돌려도 돌아오지 않는다 (`.claude/rules/data-safety.md`).

    빈 자리는 건너뛴다. 자리가 하나도 값을 내놓지 않으면 빈 문자열이고, 그것을 실패로 볼지는
    부르는 쪽이 정한다.
    """
    paths = [path] if isinstance(path, str) else list(path)
    parts: list[str] = []
    for one in paths:
        parts.extend(_texts(payload, one))
    return "\n\n".join(_unique(parts))


def _unique(parts: Iterable[str]) -> list[str]:
    """같은 값이 여러 칸에 들어 있으면 한 번만 적는다.

    `*` 로 배열을 훑으면 칸마다 값을 하나씩 얻는데, 그 값이 칸마다 같은 경우가 있다.
    한화는 모집 부문이 셋이어도 근무지가 셋 다 `서울` 이라 그대로 이으면 `서울 서울 서울`
    이 된다. 조직명도 마찬가지다.

    순서는 지킨다. 처음 나온 자리에 남는다.
    """
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        text = part.strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(part)
    return out


def _texts(payload: Any, path: str) -> list[str]:
    """경로 하나가 가리키는 값들. `*` 가 있으면 배열의 칸마다 하나씩이다."""
    head, mark, tail = path.partition(WILDCARD)
    if not mark:
        return [_text(_dig(payload, path))]

    entries = _dig(payload, head.rstrip("."))
    if not isinstance(entries, list):
        # 경로는 배열을 가리킨다고 적혀 있는데 배열이 아니다. 그 자리만 고치면 된다
        return []
    inner = tail.lstrip(".")
    return [_text(_dig(entry, inner)) if inner else _text(entry) for entry in entries]


def _describe(path: FieldPath) -> str:
    """오류 문구에 적을 경로. 여럿이면 그대로 늘어놓는다."""
    return path if isinstance(path, str) else " + ".join(path)


def _dig(payload: Any, path: str) -> Any:
    """점 표기 경로로 값을 찾는다. 못 찾으면 `MISSING` 이다.

    숫자 조각은 배열의 자리다 — `recList.0.detailContext` 는 `recList` 의 첫 항목을 본다.
    키에 점이 들어간 응답은 아직 만난 적이 없어 다루지 않는다.
    """
    current = payload
    for part in path.split("."):
        if isinstance(current, Mapping):
            if part not in current:
                return MISSING
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                return MISSING
            current = current[index]
        else:
            return MISSING
    return current


def _text(value: Any) -> str:
    """값을 문자열로 만든다. 정제는 하지 않는다 — HTML 조각도 그대로 남는다."""
    if isinstance(value, _Missing) or value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    # 객체나 배열이 온 자리다. 경로가 한 단계 얕은 것이므로 눈에 보이게 남긴다
    return json.dumps(value, ensure_ascii=False)
