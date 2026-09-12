"""셀렉터 JSON 을 HTML 에 적용해 목록 항목과 상세 필드를 뽑는다.

여기서 하는 것은 "셀렉터가 잡은 노드에서 값을 꺼내는 것"까지다. 꺼낸 텍스트는 손대지 않는다 —
앞뒤 공백, 줄바꿈, 사이에 낀 광고 문구를 여기서 지우면 지저분한 값 하나 때문에 셀렉터가
사이트 구조가 아니라 그 사이트의 텍스트에 묶인다. 정제는 정규화 규칙의 몫이다 (`CLAUDE.md`).

이 모듈은 네트워크를 모른다. HTML 문자열을 받아서 결과나 예외를 돌려줄 뿐이고, 가져오는 일은
`app/crawler/fetcher.py` 가 한다.

실패는 두 가지로 나뉘고, 조치가 다르다 (`.claude/rules/crawling.md`).

| 예외 | 뜻 | 조치 |
|---|---|---|
| `SelectorMissError` | 가져오기는 됐는데 item 셀렉터가 0개 매칭 | 재시도 금지. 셀렉터 재작성 |
| `FieldParseError` | 매칭은 됐는데 필요한 필드를 못 읽음 | 그 필드만 보정 |

목록 항목 0건은 실패다. 신규 0건인 정상 실행과 같은 결과로 남기지 않는다.

상세 링크를 어디서 뽑는지는 `app/selector/link.py` 가 정한다. `href` 를 읽는 것이 기본이고,
`list.link_template` 이 있으면 항목의 속성값으로 URL 을 만든다. 어느 쪽이든 여기서는 목록
URL 과 합쳐 절대 URL 로 만들 뿐이고, 따라가도 되는 URL 인지는 공용 fetch 클라이언트가 다시
본다.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from bs4.element import Tag
from soupsieve import SelectorSyntaxError

from app.selector.link import LinkResult, resolve_link
from app.selector.schema import DETAIL_FIELDS, DetailSelectors, ListSelectors

# 이 값이 없으면 공고를 식별할 수도, 상세로 따라갈 수도 없다. 나머지는 비어도 항목이 남는다.
REQUIRED_LIST_FIELDS: tuple[str, ...] = ("title", "link")

# 상세에서 이 둘이 비면 적재할 내용이 없다. 나머지는 사이트에 항목 자체가 없을 수 있다.
REQUIRED_DETAIL_FIELDS: tuple[str, ...] = ("title", "body")


class CrawlDataError(Exception):
    """가져오기는 성공한 뒤에 생긴 실패. `error_class` 는 `crawl_runs.error_class` 로 간다."""

    error_class = "parse"


class SelectorMissError(CrawlDataError):
    """item 셀렉터가 0개 매칭. 사이트 구조가 바뀌었거나 JS 렌더링이다."""

    error_class = "selector_miss"


class FieldParseError(CrawlDataError):
    """노드는 잡았는데 필드를 읽지 못했다."""

    error_class = "parse"


@dataclass(frozen=True)
class FieldFailure:
    """항목 하나에서 실패한 필드 하나. `index` 는 목록에서의 순번이다."""

    index: int
    field: str
    message: str


@dataclass(frozen=True)
class ListItem:
    """목록에서 뽑은 항목 하나. `link` 만 절대 URL 로 만들고 나머지는 원문 그대로다."""

    index: int
    title: str
    link: str
    date: str
    # 셀렉터가 없거나 못 찾으면 빈 문자열이다. 회사명이 없는 사이트가 흔하다
    company: str = ""
    # 상세 페이지가 없는 사이트다. 실행이 상세를 따라가지 않는다
    detail_absent: bool = False
    # 상세 API 에 넘길 공고 id. 목록이 API 면 응답의 `id_field` 값이고, HTML 이면 상세
    # 링크의 마지막 경로 조각이다. 상세가 API 가 아니면 아무도 읽지 않는다
    detail_key: str = ""
    # 목록 응답이 상세 칸의 값까지 들고 있을 때 그 값들. 키는 상세 필드 이름 그대로다.
    #
    # 카카오 목록 API 는 직군·근무지·모집인원·주요 업무·전형 절차를 항목마다 담아 주는데
    # 상세 문서에는 그것들이 한 덩어리로만 있다. 여기 싣지 않으면 그 값들은 수집 단계에서
    # 사라지고, 매핑하지 않은 값은 저장되지 않으므로 다시 얻을 길이 없다
    # (`.claude/tasks/memos/보류/split-body/prd-split-body.md`).
    #
    # 상세에서 읽은 값이 있으면 그쪽이 이긴다. 이것은 상세가 비었을 때만 쓰는 값이다
    # (`app/crawler/runner.py` 의 `_record`).
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ListParseResult:
    matched: int
    items: list[ListItem]
    failures: list[FieldFailure]


@dataclass(frozen=True)
class DetailParseResult:
    """`fields` 는 상세 셀렉터 이름 그대로다. `missing` 은 셀렉터는 있는데 0개 매칭인 선택 필드.

    `source_text` 는 상세 컨테이너를 가공 없이 편 글자다. 페이지에 검색엔진용 `JobPosting`
    데이터가 있으면 컨테이너에 없는 값을 뒤에 붙인다 (`structured_text`). 뽑지 못하면 빈
    문자열이고, 그 건은
    지금까지와 같은 모양으로 적재된다 (`app/crawler/runner.py` 의 `_record`). API 상세는
    응답 전체를 편다 (`app/crawler/api_source.py` 의 `payload_source_text`).
    """

    fields: dict[str, str]
    missing: list[str]
    source_text: str = ""


def list_only(selectors: ListSelectors) -> bool:
    """상세로 갈 길이 아예 없는 사이트인가.

    `link` 와 `link_template` 이 둘 다 비어 있으면 모델이 "이 목록에는 상세 링크가 없다" 고
    답한 것이다. 삼성처럼 상세를 JS 로 그려 별도 주소가 없는 사이트가 그렇다.

    셀렉터가 비어 있는 것과, 셀렉터가 있는데 0개 매칭인 것은 다르다. 앞의 것은 없는 것이고
    뒤의 것은 실패다 — 화면의 `건너뜀` / `실패` 구분과 같은 기준이다.
    """
    return not selectors.link.strip() and not selectors.link_template.strip()


def parse_list(html: str, selectors: ListSelectors, base_url: str) -> ListParseResult:
    """목록 페이지에서 항목을 뽑는다. 0개 매칭은 `SelectorMissError` 다.

    상세 링크가 없는 사이트는 목록에서 읽은 것만으로 항목을 만든다. `link` 는 목록 페이지
    주소가 되고, 상세는 따라가지 않는다.
    """
    soup = BeautifulSoup(html, "html.parser")
    nodes = select_nodes(soup, selectors.item, "list.item")
    if not nodes:
        raise SelectorMissError(
            f"list.item `{selectors.item}` 이 0개 매칭됐다. 사이트 구조가 바뀌었거나 JS 렌더링이다"
        )

    items: list[ListItem] = []
    failures: list[FieldFailure] = []
    link_absent = list_only(selectors)

    for index, node in enumerate(nodes):
        title = field_text(node, selectors.title, f"list.title[{index}]")
        link = _link(node, selectors, index)
        date = field_text(node, selectors.date, f"list.date[{index}]")
        company = (
            field_text(node, selectors.company, f"list.company[{index}]")
            if selectors.company
            else ""
        )

        problems: list[FieldFailure] = []
        if not title:
            problems.append(
                FieldFailure(
                    index=index, field="title", message="셀렉터가 항목 안에서 값을 찾지 못했다"
                )
            )
        if not link.ok and not link_absent:
            # 링크는 왜 못 뽑았는지가 조치를 가른다. href 가 없는 것과 속성이 없는 것은
            # 다른 문제다 (`app/selector/link.py`)
            problems.append(FieldFailure(index=index, field="link", message=link.reason))
        if problems:
            failures.extend(problems)
            continue

        items.append(
            ListItem(
                index=index,
                title=title,
                # 상세로 갈 길이 없으면 목록 주소를 남긴다. 공고를 가리키는 주소는 그것뿐이고,
                # 같은 값이어도 content_hash 는 title·deadline·body 로 공고를 가른다
                link=base_url if link_absent else urljoin(base_url, link.url),
                date=date,
                company=company,
                detail_absent=link_absent,
            )
        )

    if not items:
        # 실제로 못 읽은 필드만 적는다. 필수 필드 이름을 통째로 적으면 title 은 멀쩡한데
        # link 만 없는 사이트에서 운영자가 두 필드를 다 뒤지게 된다
        unread = [name for name in REQUIRED_LIST_FIELDS if any(f.field == name for f in failures)]
        # 첫 항목의 사유까지 붙인다. 이 문구가 crawl_runs.error_message 로 남아서, 다음
        # 사람이 실행을 다시 돌리지 않고도 무엇이 없었는지 알게 된다
        raise FieldParseError(
            f"item {len(nodes)}건을 잡았지만 어느 항목에서도 {', '.join(unread)} 를 "
            f"읽지 못했다: {failures[0].message}"
        )

    return ListParseResult(matched=len(nodes), items=items, failures=failures)


def parse_detail(html: str, selectors: DetailSelectors) -> DetailParseResult:
    """상세 페이지에서 필드를 뽑는다. 필수 필드를 못 읽으면 `FieldParseError` 다."""
    soup = BeautifulSoup(html, "html.parser")
    fields: dict[str, str] = {}
    missing: list[str] = []

    for name in DETAIL_FIELDS:
        selector = getattr(selectors, name)
        if not selector:
            # 사이트에 그 항목이 없다는 응답이다. 빈 값이지 실패가 아니다.
            fields[name] = ""
            continue

        value = field_text(soup, selector, f"detail.{name}")
        fields[name] = value
        if not value:
            missing.append(name)

    unreadable = [name for name in REQUIRED_DETAIL_FIELDS if not fields[name]]
    if unreadable:
        raise FieldParseError(f"상세에서 필수 필드를 읽지 못했다: {', '.join(unreadable)}")

    container = source_text(soup, selectors.body)
    structured = structured_text(soup, container) if container.strip() else ""
    return DetailParseResult(
        fields=fields,
        missing=missing,
        source_text=f"{container}\n{structured}" if structured else container,
    )


def select_nodes(scope: BeautifulSoup | Tag, selector: str, name: str) -> list[Tag]:
    """셀렉터가 잡은 노드들. 문법 오류는 어느 필드였는지를 붙여 올린다.

    JSON API 경로도 이 함수를 쓴다. 삼성 목록은 API 로 물어보는데 응답이 HTML 조각이라,
    같은 판정과 같은 오류 문구를 지나야 한다 (`app/crawler/api_source.py`).
    """
    try:
        return list(scope.select(selector))
    except SelectorSyntaxError as exc:
        raise FieldParseError(f"{name} 셀렉터 문법 오류: {exc}") from exc


# 줄이 바뀌어야 하는 태그. 이 목록에 없는 것(strong, span, a 같은 인라인)은 앞뒤 글자와
# 이어져야 한다 — 거기까지 줄을 넣으면 문장 하나가 여러 줄로 쪼개진다.
BLOCK_TAGS: frozenset[str] = frozenset(
    {
        "address",
        "article",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "hr",
        "li",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)


def field_text(scope: BeautifulSoup | Tag, selector: str, name: str) -> str:
    """첫 매칭 노드의 텍스트. 매칭이 없으면 빈 문자열이다.

    블록 태그 경계에 줄바꿈을 넣고 뽑는다. `get_text()` 를 그냥 부르면 `<h3>조직소개</h3>`
    와 뒤따르는 `<p>` 가 한 줄로 이어붙어, 본문 전체가 한 문단처럼 보인다. 실제로
    현대자동차 공고 본문 1,955자가 그렇게 들어왔다.

    이것을 정규화로는 고칠 수 없다. 정규화가 값을 받는 시점에는 어디가 문단 경계였는지가
    이미 사라진 뒤다. 구조를 아는 것은 여기뿐이다.

    남는 빈 줄은 여기서 정리하지 않는다. `\n{3,}` 를 줄이는 것은 정규화 규칙의 일이다.
    """
    if not selector.strip():
        # 셀렉터가 비어 있다. 모델이 "사이트에 그 항목이 없다" 고 답한 자리이고 문법 오류가
        # 아니다. 빈 값으로 두지 않고 오류로 만들면 목록 전체를 못 읽게 된다 —
        # 네이버 등록이 `list.date` 하나가 비었다는 이유로 항목 0건이 됐다
        return ""

    nodes = select_nodes(scope, selector, name)
    if not nodes:
        return ""
    return block_text(nodes[0])


def block_text(node: Tag) -> str:
    """블록 태그 경계에 줄바꿈을 넣고 뽑은 텍스트.

    원본을 복사해서 손댄다. 같은 트리에서 다른 필드도 뽑으므로 트리를 바꾸면 안 된다.
    """
    copied = copy.copy(node)
    for tag in copied.find_all(BLOCK_TAGS):
        tag.insert_before("\n")
        tag.insert_after("\n")
    return copied.get_text()


# 상세 컨테이너 안에 남아 있어도 이 공고의 내용이 아닌 것. 카카오 상세의 `div.aside_board`
# 가 같은 직군의 다른 공고 열한 건을 담고 있었고, 그것이 열한 픽스처에서 이 목록이 걸린
# 유일한 자리다 (`.claude/site-recipes/source-text-container.md`).
PAGE_FURNITURE: str = "header, nav, footer, aside, [class*=aside], [class*=footer], [class*=gnb]"


def source_text(soup: BeautifulSoup, body_selector: str) -> str:
    """상세 원문. 본문 셀렉터가 잡은 노드의 **부모**를 가공 없이 편 글자다.

    본문만 담으면 이름표로 붙은 값(회사·마감·근무지·경력)이 원문 밖에 남고, 조상을 더
    올리면 목록 화면의 필터와 다른 공고가 섞인다 — 네이버는 조상 3단계에서 3,880자가
    6,797자가 됐다. 열한 픽스처를 재고 부모 한 단계로 정했다
    (`.claude/site-recipes/source-text-container.md`).

    페이지 부속은 뺀다. 원문은 근거 검사가 "이 글자가 있나" 로 읽는 값이라, 푸터의 회사
    주소나 옆에 붙은 다른 공고의 근무지가 이 공고의 값으로 통과하면 안 된다
    (`.claude/tasks/todo/prd-side-workflows.md` 4절).

    부모가 `body` 나 `html` 이면 본문 노드를 그대로 쓴다. 페이지 전체가 원문이 되면 GNB 와
    푸터가 통째로 들어온다 — 잰 일곱 곳에서 그 글자가 730자에서 5,456자였다.

    뽑지 못하면 빈 문자열이다. 원문이 없다고 공고를 버리지 않는다.
    """
    if not body_selector.strip():
        return ""

    nodes = select_nodes(soup, body_selector, "detail.body")
    if not nodes:
        return ""

    body = nodes[0]
    parent = body.parent
    if parent is None or parent.name in ("body", "html", "[document]"):
        return block_text(body)

    container = copy.copy(parent)
    for tag in container.select(PAGE_FURNITURE):
        tag.decompose()
    return block_text(container)


def _link(node: Tag, selectors: ListSelectors, index: int) -> LinkResult:
    """상세 링크. 뽑는 방식과 실패 사유는 `app/selector/link.py` 가 정한다."""
    try:
        return resolve_link(node, selectors)
    except SelectorSyntaxError as exc:
        raise FieldParseError(f"list.link[{index}] 셀렉터 문법 오류: {exc}") from exc


# 검색엔진이 읽는 표준 채용공고 데이터의 형 이름 (schema.org)
_JOB_POSTING = "JobPosting"
_SPACES = re.compile(r"\s+")


def structured_text(soup: BeautifulSoup, known: str) -> str:
    """페이지의 JSON-LD `JobPosting` 값을 `키: 값` 줄로 편 글자. 붙일 것이 없으면 빈 문자열이다.

    화면 글자에는 없고 이 데이터에만 있는 값이 있다 — 네이버는 근무지 주소(`streetAddress`)와
    고용형태(`employmentType: INTERN`)가 그렇다. 원문 컨테이너를 조상으로 넓히는 대신 이것을
    붙인다. 일곱 픽스처에서 조상으로 올라가면 메뉴·검색 필터·푸터만 늘었다 (2026-09-12 측정).

    `@` 로 시작하는 키(`@type`, `@context`)는 스키마의 표시라 빼고, `known`(컨테이너 원문)에 이미
    있는 값은 다시 적지 않는다 — 제목과 본문을 되풀이한 `description` 이 그렇다. 코드값은 거르지
    않는다. 고용형태가 `INTERN` 같은 대문자 코드로 온다. 읽지 못하는 JSON 은 건너뛴다.
    """
    seen = _compact(known)
    added: set[str] = set()
    lines: list[str] = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.get_text())
        except ValueError:
            continue
        for posting in _job_postings(data):
            for key, value in _structured_leaves(posting):
                text = BeautifulSoup(value, "html.parser").get_text("\n") if "<" in value else value
                for raw in text.splitlines():
                    line = raw.strip()
                    compact = _compact(line)
                    if not compact or compact in seen or compact in added:
                        continue
                    added.add(compact)
                    lines.append(f"{key}: {line}")
    return "\n".join(lines)


def _job_postings(data: Any) -> Iterator[Mapping[str, Any]]:
    """JSON-LD 안의 `JobPosting` 객체들. 목록이나 `@graph` 안에 들어 있기도 하다."""
    if isinstance(data, list):
        for item in data:
            yield from _job_postings(item)
    elif isinstance(data, Mapping):
        kinds = data.get("@type")
        if kinds == _JOB_POSTING or (isinstance(kinds, list) and _JOB_POSTING in kinds):
            yield data
        yield from _job_postings(data.get("@graph", []))


def _structured_leaves(value: Any, key: str = "") -> Iterator[tuple[str, str]]:
    """값을 차례대로. 값마다 바로 위 키 이름이 붙는다. `@` 키와 빈 값은 건너뛴다."""
    if isinstance(value, Mapping):
        for name, inner in value.items():
            if not str(name).startswith("@"):
                yield from _structured_leaves(inner, str(name))
    elif isinstance(value, list):
        for inner in value:
            yield from _structured_leaves(inner, key)
    elif isinstance(value, str | int | float) and not isinstance(value, bool):
        text = str(value).strip()
        if text:
            yield key, text


def _compact(text: str) -> str:
    """공백과 대소문자를 뺀 글자. 이미 원문에 있는 값인지 볼 때만 쓴다."""
    return _SPACES.sub("", text).casefold()
