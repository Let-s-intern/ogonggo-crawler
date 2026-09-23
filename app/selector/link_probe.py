"""클릭해서 알아낸 주소를 **공고마다 다른 주소 형식** 으로 옮긴다.

`app/selector/detail_path.py` 는 클릭 뒤 나간 요청 하나를 상세 API 설정으로 옮긴다. 여기서
하는 것은 그 앞 단계다 — 상세가 JSON API 가 아니라 그냥 HTML 문서인데, 그 문서로 가는 주소를
항목이 `href` 로 들고 있지 않은 사이트를 위한 자리다.

두산과 네이버가 그렇다.

| 사이트 | 항목이 들고 있는 것 | 클릭하면 |
|---|---|---|
| 두산 | `onclick="goDetail('1000361539', 'C_REC_MGT_04', ...)"` | 같은 주소로 폼 POST 가 나간다 |
| 네이버 | `onclick="show('30005276')"` | `view.do?annoId=30005276` 으로 이동한다 |

둘 다 `href` 는 `javascript:void(0)` 라 따라갈 수 없고, 클릭으로 알아낸 주소 하나를 저장해도
그것은 **첫 공고의 주소** 일 뿐이다. 그 주소를 그대로 두면 공고가 몇 건이든 같은 상세를
가져온다.

## 하는 일

알아낸 주소 안에서 **그 항목이 들고 있던 값** 을 찾아 자리표시자로 바꾼다.

    https://recruit.navercorp.com/rcrt/view.do?annoId=30005276
    -> https://recruit.navercorp.com/rcrt/view.do?annoId={onclick|arg1}

바꿀 값은 항목 안의 `data-` 속성값과 `onclick`·`href` 안의 따옴표 인자에서만 가져온다. 그
값들은 항목마다 다르고, 그래서 공고마다 다른 주소가 나온다. 어디에도 없는 값은 그대로 둔다 —
그것은 사이트가 늘 같이 보내는 상수다.

폼 POST 로 나간 요청은 본문을 쿼리로 붙여 GET 주소로 만든다. 되는지는 확인이 말해 준다.

## 확인 없이 채택하지 않는다

만든 형식으로 **항목 두 개의 주소를 만들어 각각 가져와서**, 그 항목의 제목이 응답 안에 있을
때만 채택한다. 한 건만 보면 항목마다 다른 값을 상수로 굳혀 놓고도 통과한다 — 첫 항목은 맞고
나머지가 전부 같은 페이지를 가리키는 상태가 그것이다.

확인은 공용 fetch 클라이언트로 한다. 브라우저에서만 되는 주소를 저장하면 등록만 성공하고
이후 실행이 전부 실패한다 (`.claude/rules/crawling.md`).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from bs4.element import Tag

from app.crawler.fetcher import FetchError, FetchPolicy
from app.crawler.playwright import ObservedRequest
from app.selector.link import ARG_MARK, js_argument, resolve_link
from app.selector.schema import ListSelectors

logger = logging.getLogger(__name__)

# 자리표시자로 쓸 값의 최소 길이. 짧은 값은 주소 아무 자리에나 우연히 들어 있다
MIN_VALUE_LENGTH = 3

# 항목 안에서 값을 읽을 속성. `data-` 로 시작하는 것은 값 전체를, 이 둘은 안의 인자를 본다
JS_ATTRIBUTES: tuple[str, ...] = ("onclick", "href")

# 항목 하나에서 훑을 자식 노드 수. 목록 항목 하나가 이보다 크면 값을 더 찾아도 소용없다
MAX_ELEMENTS = 40

# 확인할 항목 수. 하나만 보면 항목마다 다른 값을 상수로 굳힌 형식도 통과한다
CONFIRM_ITEMS = 2

# 값을 읽지 않을 노드의 표시. SNS 공유 버튼은 공고 번호를 인자로 갖고 있지만 상세로 가지 않는다
SHARE_MARK = "share"


@dataclass(frozen=True)
class ValueSource:
    """항목 안에서 값 하나를 읽는 법.

    `selector` 는 항목 안에서 그 노드를 찾는 셀렉터이고, 비어 있으면 항목 노드 자신이다.
    `placeholder` 는 `link_template` 에 그대로 들어가는 `{onclick|arg1}` 같은 표기다.
    """

    selector: str
    placeholder: str
    value: str


@dataclass(frozen=True)
class LinkProposal:
    """공고마다 다른 상세 주소를 만드는 법. `reason` 이 비어 있을 때만 쓸 수 있다.

    `selector` 와 `template` 이 그대로 `list.link` 와 `list.link_template` 이 된다.
    """

    selector: str = ""
    template: str = ""
    resolved: int = 0
    count: int = 0
    source: str = ""
    # 확인하면서 실제로 열어 본 첫 공고의 주소. 상세 셀렉터를 만들 때 이 페이지를 본다
    sample: str = ""
    notes: tuple[str, ...] = ()
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.reason and bool(self.template)

    def selectors(self, base: ListSelectors) -> ListSelectors:
        """이 제안을 얹은 목록 셀렉터. 저장 여부는 부르는 쪽이 정한다."""
        return base.model_copy(update={"link": self.selector, "link_template": self.template})


@dataclass(frozen=True)
class LinkConfirmation:
    """만든 형식으로 실제 주소를 만들어 가져와 본 결과."""

    adopted: bool
    reason: str = ""
    checked: int = 0
    urls: tuple[str, ...] = ()


def propose_link_template(
    nodes: Sequence[Tag],
    *,
    reached_url: str,
    list_url: str,
    requests: Sequence[ObservedRequest] = (),
    navigations: Sequence[str] = (),
) -> LinkProposal:
    """클릭이 데려간 주소나 그때 나간 요청을 항목마다 다른 주소 형식으로 옮긴다.

    후보는 순서가 있다. 클릭이 실제로 도착한 주소가 먼저이고, 주소가 그대로인 사이트(두산)
    에서만 나간 요청을 본다.
    """
    if not nodes:
        return LinkProposal(reason="렌더된 항목이 없어 값을 읽을 자리가 없다")

    sources = value_sources(nodes[0])
    if not sources:
        return LinkProposal(
            reason=(
                "첫 항목에 공고를 지목할 값이 없다. `data-` 속성도, `onclick` 인자도 없어 "
                "주소를 만들 재료가 없다"
            )
        )

    for candidate, origin in _candidates(reached_url, list_url, requests, navigations):
        selector, template, used = _templatize(candidate, sources)
        if not used:
            continue
        return LinkProposal(
            selector=selector,
            template=template,
            count=len(nodes),
            resolved=_resolved(nodes, selector, template),
            source=origin,
            notes=tuple(f"{one.placeholder} = {one.value}" for one in used),
        )

    return LinkProposal(
        reason=(
            "클릭으로 알아낸 주소 안에 이 항목의 값이 하나도 없다. 공고마다 달라지는 자리를 "
            "찾지 못해 주소 형식을 만들 수 없다"
        )
    )


async def confirm_link_template(
    client: FetchPolicy, proposal: LinkProposal, nodes: Sequence[Tag], titles: Sequence[str]
) -> LinkConfirmation:
    """만든 형식으로 항목 두 개의 주소를 만들어 가져와 제목이 있는지 본다."""
    if not proposal.ok:
        return LinkConfirmation(adopted=False, reason="주소 형식이 없다. 확인할 것이 없다")

    selectors = ListSelectors(
        item="",
        title="",
        link=proposal.selector,
        date="",
        link_template=proposal.template,
    )
    checked: list[str] = []
    for node, title in list(zip(nodes, titles, strict=False))[:CONFIRM_ITEMS]:
        if not title.strip():
            continue
        link = resolve_link(node, selectors)
        if not link.ok:
            return LinkConfirmation(
                adopted=False,
                reason=f"항목 {len(checked) + 1}번의 주소를 만들지 못했다: {link.reason}",
                checked=len(checked),
                urls=tuple(checked),
            )
        try:
            result = await client.fetch(link.url)
        except FetchError as exc:
            return LinkConfirmation(
                adopted=False,
                reason=f"만든 주소 {link.url} 를 공용 fetch 클라이언트로 열지 못했다: {exc}",
                checked=len(checked),
                urls=tuple(checked),
            )
        if _squeeze(title) not in _squeeze(result.text):
            return LinkConfirmation(
                adopted=False,
                reason=(
                    f"만든 주소 {link.url} 를 열었지만 그 공고의 제목 `{title}` 이 없다. "
                    "항목마다 달라지는 자리를 잘못 짚었거나 상세가 JS 로 그려진다"
                ),
                checked=len(checked),
                urls=(*checked, link.url),
            )
        checked.append(link.url)

    if len(checked) < CONFIRM_ITEMS:
        return LinkConfirmation(
            adopted=False,
            reason=(
                f"확인한 항목이 {len(checked)}건뿐이다. 두 건을 견주지 않으면 공고마다 다른 "
                "주소가 나오는지 알 수 없다"
            ),
            checked=len(checked),
            urls=tuple(checked),
        )

    logger.info("상세 주소 형식 채택 template=%s 확인=%d건", proposal.template, len(checked))
    return LinkConfirmation(adopted=True, checked=len(checked), urls=tuple(checked))


def value_sources(node: Tag) -> list[ValueSource]:
    """항목 하나에서 읽을 수 있는 값들. 항목 노드가 먼저고 그다음이 자식이다."""
    found: list[ValueSource] = []
    seen: set[tuple[str, str]] = set()

    for element in [node, *node.find_all(True, limit=MAX_ELEMENTS)]:
        if not isinstance(element, Tag):
            continue
        if element is not node and _shares(element):
            # SNS 공유 버튼이다. 공고 번호가 인자에 들어 있지만 이것으로 주소를 만들면
            # 공유 링크의 다른 인자까지 자리표시자로 끌려 들어온다
            continue
        selector = "" if element is node else _selector_for(node, element)
        if selector is None:
            continue
        for name, raw in element.attrs.items():
            key = str(name)
            value = " ".join(raw) if isinstance(raw, list) else str(raw or "")
            value = value.strip()
            if key.startswith("data-") and _usable(value):
                _add(found, seen, ValueSource(selector, f"{{{key}}}", value))
            if element.name == "input" and key == "value" and _usable(value):
                # 숨은 입력값에 공고 번호를 두고 클릭 때 읽어 주소를 만드는 사이트가 있다.
                # GC녹십자: `<input type="hidden" class="key" value="2859">` (2026-09-22)
                _add(found, seen, ValueSource(selector, "{value}", value))
            if key == "href" and _usable(value) and not value.lower().startswith("javascript:"):
                # 항목 자체가 `a` 인 사이트가 있다. 카카오 목록이 `<a><li>...</li></a>` 라
                # 항목 안에서 링크를 찾는 셀렉터로는 주소가 나오지 않고, 그때는 그 `href`
                # 통째가 상세 주소다
                _add(found, seen, ValueSource(selector, f"{{{key}}}", value))
            if key not in JS_ATTRIBUTES:
                continue
            for index in range(1, 6):
                argument = js_argument(value, index)
                if not argument:
                    break
                if _usable(argument) or _usable_number(argument):
                    _add(
                        found,
                        seen,
                        ValueSource(selector, f"{{{key}{ARG_MARK}{index}}}", argument),
                    )
    return found


def _shares(element: Tag) -> bool:
    """SNS 공유 링크인가.

    클릭할 때의 판정(`app/crawler/click_probe.py` 의 `is_share_link`)보다 좁다. 그쪽은
    `javascript:` 로 시작하는 `href` 를 통째로 건너뛰는데, 여기서 필요한 값은 바로 그
    `javascript:` 안에 들어 있다 — 두산의 `a.list-tit` 이 `href="javascript:void(0);"` 이고
    공고 번호는 그 노드의 `onclick` 인자다. 누르지 않고 읽기만 하므로 이름으로만 거른다.
    """
    marks = (str(element.get("href") or ""), str(element.get("onclick") or ""))
    return any(SHARE_MARK in mark.lower() for mark in marks)


def _add(found: list[ValueSource], seen: set[tuple[str, str]], source: ValueSource) -> None:
    key = (source.selector, source.value)
    if key in seen:
        return
    seen.add(key)
    found.append(source)


def _candidates(
    reached_url: str,
    list_url: str,
    requests: Sequence[ObservedRequest],
    navigations: Sequence[str] = (),
) -> list[tuple[str, str]]:
    """주소 형식으로 만들어 볼 후보들. (주소, 어디서 왔는지) 순서대로."""
    found: list[tuple[str, str]] = []
    if reached_url.strip() and reached_url != list_url:
        found.append((reached_url, "클릭이 도착한 주소"))
    for url in navigations:
        # 리다이렉트 전 주소다. 도착한 주소에는 공고 번호가 없어도 여기에는 있다(GC녹십자)
        if url.strip() and url not in (list_url, reached_url):
            found.append((url, "클릭 뒤 이동한 주소"))
    for request in requests:
        if request.status != 200:
            continue
        url = _as_get(request)
        if url and url != list_url:
            found.append((url, f"클릭 뒤 나간 {request.method} 요청"))
    return found


def _as_get(request: ObservedRequest) -> str:
    """요청 하나를 GET 주소로. 폼 본문은 쿼리로 붙인다.

    두산 상세가 목록과 같은 주소로 폼 POST 를 보내는데, 같은 값을 쿼리로 붙인 GET 도 같은
    문서를 준다. 되는지는 확인이 판정한다 — 여기서는 후보를 만들 뿐이다.
    """
    body = request.request_body.strip()
    if request.method != "POST":
        return request.url
    if not body or body.startswith(("{", "[")) or "=" not in body:
        # JSON 본문으로 나간 요청은 GET 주소로 옮길 수 없다
        return ""
    separator = "&" if urlsplit(request.url).query else "?"
    return f"{request.url}{separator}{body}"


def _templatize(url: str, sources: Sequence[ValueSource]) -> tuple[str, str, list[ValueSource]]:
    """주소 안의 항목 값을 자리표시자로 바꾼다. 한 노드에서 읽은 값만 섞어 쓴다.

    자리표시자는 `link_template` 이 한 노드의 속성만 읽기 때문에 셀렉터가 하나여야 한다
    (`app/selector/link.py`). 그래서 셀렉터별로 만들어 보고 가장 많이 바뀐 것을 고른다.
    """
    # 호스트는 건드리지 않는다. `naver` 같은 짧은 값이 도메인 안에 우연히 들어 있고, 그것을
    # 자리표시자로 바꾸면 주소 자체가 무너진다
    parts = urlsplit(url)
    head = f"{parts.scheme}://{parts.netloc}"
    tail = url[len(head) :] if url.startswith(head) else url

    best: tuple[int, int, str, str, list[ValueSource]] | None = None
    order = {
        selector: index for index, selector in enumerate(source.selector for source in sources)
    }
    for selector in dict.fromkeys(source.selector for source in sources):
        group = sorted(
            (source for source in sources if source.selector == selector),
            key=lambda source: -len(source.value),
        )
        text = tail
        used: list[ValueSource] = []
        for source in group:
            if source.value in text:
                text = text.replace(source.value, source.placeholder)
                used.append(source)
        # 많이 바뀐 것이 먼저고, 같으면 항목에서 먼저 나온 노드가 먼저다
        score = (-len(used), order[selector])
        if used and (best is None or score < (-best[0], best[1])):
            best = (len(used), order[selector], selector, head + text, used)

    if best is None:
        return "", "", []
    _, _, selector, template, used = best
    return selector, template, used


def _resolved(nodes: Sequence[Tag], selector: str, template: str) -> int:
    """이 형식으로 주소가 나오는 항목이 몇 건인가. 실행 때와 같은 함수로 센다."""
    selectors = ListSelectors(item="", title="", link=selector, date="", link_template=template)
    return sum(1 for node in nodes if resolve_link(node, selectors).ok)


def _selector_for(item: Tag, element: Tag) -> str | None:
    """항목 안에서 그 노드를 찾는 셀렉터. 못 만들면 None 이고 그 노드는 건너뛴다."""
    raw = element.get("class")
    classes = raw if isinstance(raw, list) else ([raw] if isinstance(raw, str) else [])
    usable = [str(name) for name in classes if _plain(str(name))]
    candidates = [element.name + "".join(f".{name}" for name in usable)] if usable else []
    candidates.append(element.name)
    for candidate in candidates:
        found = item.select(candidate)
        if found and found[0] is element:
            return candidate
    return None


def _plain(name: str) -> bool:
    """셀렉터에 그대로 적을 수 있는 클래스명인가."""
    return bool(name.strip()) and all(char.isalnum() or char in "-_" for char in name)


def _usable(value: str) -> bool:
    return len(value) >= MIN_VALUE_LENGTH


# 숫자 인자로 받는 최소 자릿수. `view(83)` 처럼 짧은 공고 번호가 있다(카페스, 2026-09-22).
# 우연히 겹친 자리를 바꿔도 두 건을 열어 제목을 대조하는 확인에서 떨어진다
MIN_NUMBER_LENGTH = 2


def _usable_number(value: str) -> bool:
    return value.isdigit() and len(value) >= MIN_NUMBER_LENGTH


def _squeeze(value: str) -> str:
    return " ".join(value.split())
