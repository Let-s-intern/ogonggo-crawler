"""항목 셀렉터가 공고가 아닌 것까지 잡았을 때 좁힌다.

네이버 목록에서 `li.item` 은 144건 잡히고 그중 공고는 10건이다. 나머지는 필터와 네비게이션
이다. 넓은 쪽을 그대로 저장하면 실행마다 공고가 아닌 항목 134건이 실패로 쌓이고, 화면의
매칭 개수는 크기만 크고 아무것도 말해 주지 않는다.

## 좁히는 기준은 제목이다

공고 하나에는 제목이 하나 있다. 그래서 **제목을 하나씩만 품는 반복 요소** 가 항목이다.
좁히는 순서는 둘이다.

| 순서 | 후보 | 언제 맞는가 |
|---|---|---|
| 1 | `<모델이 낸 항목>:has(<제목>)` | 잡은 것 중 제목 없는 것만 걸러내면 되는 경우 |
| 2 | 제목의 조상 중 가장 가까운 반복 요소 | 항목 셀렉터가 아예 다른 것을 잡은 경우 |

1번이 먼저인 것은 그것이 모델이 낸 셀렉터를 가장 적게 바꾸기 때문이다. 항목 안에서 찾는
`link`·`date`·`company_name` 셀렉터가 그대로 살아 있어야 좁힌 것이 이득이 되고, 그래서 후보는
그 필드들을 여전히 품고 있을 때만 채택한다.

**넓히지 않는다.** 매칭이 늘어나는 방향으로는 절대 바꾸지 않고, 후보가 제목을 하나라도
빠뜨리면 그대로 둔다. 페이지 전체를 잡는 셀렉터는 조용히 실패하기 때문에 0개 매칭보다 나쁘다
(`.claude/agents/selector-worker.md`).

바꿨으면 무엇을 무엇으로 바꿨는지 문장으로 남긴다. 운영자가 모델이 낸 셀렉터를 보고 있다고
생각하는데 다른 것이 저장돼 있으면, 다음에 깨졌을 때 아무도 설명하지 못한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bs4 import BeautifulSoup
from bs4.element import Tag
from soupsieve import SelectorSyntaxError

from app.selector.schema import SelectorSet

logger = logging.getLogger(__name__)

# 반복으로 볼 최소 제목 수. 공고가 한 건인 목록에서는 무엇이 반복인지 말할 수 없다
MIN_TITLES = 2

# 제목에서 몇 단계까지 거슬러 올라가며 항목을 찾는가. 이보다 위는 목록 전체를 감싸는 쪽이다
MAX_STEPS = 4

# 항목 안에서 찾는 나머지 셀렉터. 좁힌 항목이 이것들을 여전히 품어야 한다
INNER_FIELDS: tuple[str, ...] = ("link", "date", "company_name")


@dataclass(frozen=True)
class Narrowing:
    """좁힌 결과. `note` 가 비어 있으면 아무것도 바꾸지 않았다는 뜻이다."""

    selectors: SelectorSet
    note: str = ""


def narrow_item_selector(selectors: SelectorSet, list_html: str) -> Narrowing:
    """항목 셀렉터가 제목 없는 노드까지 잡고 있으면 제목이 있는 반복 요소로 좁힌다."""
    item = selectors.list.item.strip()
    title = selectors.list.title.strip()
    if not item or not title:
        return Narrowing(selectors)

    soup = BeautifulSoup(list_html, "html.parser")
    try:
        titles = soup.select(title)
        current = soup.select(item)
    except SelectorSyntaxError:
        # 문법 오류는 자체 검증이 필드 실패로 적는다. 여기서 고치지 않는다
        return Narrowing(selectors)

    if len(titles) < MIN_TITLES or not current:
        return Narrowing(selectors)

    complete = [node for node in current if len(node.select(title)) == 1]
    if len(complete) == len(current):
        # 잡은 것이 전부 제목을 하나씩 갖고 있다. 좁힐 이유가 없다
        return Narrowing(selectors)

    candidate = _pick(soup, selectors, titles, complete)
    if not candidate:
        return Narrowing(selectors)

    matched = len(soup.select(candidate))
    logger.info("항목 셀렉터를 좁힌다 %s(%d건) -> %s(%d건)", item, len(current), candidate, matched)
    narrowed = selectors.list.model_copy(update={"item": candidate})
    return Narrowing(
        selectors=selectors.model_copy(update={"list": narrowed}),
        note=(
            f"항목 셀렉터 `{item}` 이 {len(current)}건을 잡았는데 그중 제목이 있는 것은 "
            f"{len(complete)}건뿐이라 `{candidate}` 로 좁혔다. 제목 {len(titles)}건과 같은 수다"
        ),
    )


def _pick(
    soup: BeautifulSoup, selectors: SelectorSet, titles: list[Tag], complete: list[Tag]
) -> str:
    """좁힐 셀렉터 하나. 못 찾으면 빈 문자열이고, 그때는 모델이 낸 것을 그대로 둔다."""
    item = selectors.list.item.strip()
    title = selectors.list.title.strip()
    inner = [
        getattr(selectors.list, name).strip()
        for name in INNER_FIELDS
        if getattr(selectors.list, name).strip()
    ]
    # 지금 항목이 실제로 품고 있는 필드만 요구한다. 원래 없던 필드를 요구하면 좁힐 수 있는
    # 사이트까지 못 좁힌다. 품은 항목이 하나도 없으면 아는 것이 없으므로 전부 요구했다가,
    # 후보가 없으면 요구를 풀고 다시 본다
    required = (
        [one for one in inner if any(node.select(one) for node in complete)] if complete else inner
    )

    for wanted in (required, []):
        scoped = f"{item}:has({title})"
        if _acceptable(soup, scoped, titles, title, wanted):
            return scoped
        found = _repeating_ancestor(soup, titles, title, wanted)
        if found:
            return found
    return ""


def _repeating_ancestor(
    soup: BeautifulSoup, titles: list[Tag], title_selector: str, required: list[str]
) -> str:
    """제목들을 하나씩만 품는 가장 가까운 조상의 셀렉터. 못 찾으면 빈 문자열."""
    for step in range(1, MAX_STEPS + 1):
        nodes = [_ancestor(node, step) for node in titles]
        found = [node for node in nodes if node is not None]
        if len(found) != len(titles):
            return ""
        if len({id(node) for node in found}) != len(titles):
            # 같은 조상 아래로 모였다. 여기부터는 항목이 아니라 목록 전체다
            return ""
        for selector in _candidates(found[0]):
            if _acceptable(soup, selector, titles, title_selector, required):
                return selector
    return ""


def _acceptable(
    soup: BeautifulSoup,
    selector: str,
    titles: list[Tag],
    title_selector: str,
    required: list[str],
) -> bool:
    """이 셀렉터로 바꿔도 되는가. 제목을 하나씩, 빠뜨리지 않고, 필드를 품은 채로 잡아야 한다."""
    try:
        matched = soup.select(selector)
    except SelectorSyntaxError:
        return False
    if not matched or len(matched) > len(titles):
        return False
    if any(len(node.select(title_selector)) != 1 for node in matched):
        return False
    if len(matched) != len(titles):
        # 제목 하나라도 이 셀렉터 밖에 있으면 그 공고를 통째로 잃는다
        return False
    return all(node.select(one) for node in matched for one in required)


def _ancestor(node: Tag, step: int) -> Tag | None:
    """`step` 만큼 위의 조상. 문서 밖으로 나가면 없다."""
    current: Tag | None = node
    for _ in range(step):
        parent = current.parent if current is not None else None
        current = parent if isinstance(parent, Tag) and parent.name != "[document]" else None
        if current is None:
            return None
    return current


def _candidates(node: Tag) -> list[str]:
    """이 노드를 가리킬 셀렉터 후보. 클래스가 없으면 부모를 붙여 자리를 좁힌다."""
    own = _own(node)
    found = [own]
    parent = node.parent
    if isinstance(parent, Tag) and parent.name != "[document]":
        found.append(f"{_own(parent)} > {own}")
    return found


def _own(node: Tag) -> str:
    """태그 이름 + 클래스 전부. 클래스가 없으면 태그 이름뿐이다."""
    raw = node.get("class")
    classes = raw if isinstance(raw, list) else ([raw] if isinstance(raw, str) else [])
    usable = [str(name) for name in classes if str(name).strip() and _plain(str(name))]
    return node.name + "".join(f".{name}" for name in usable)


def _plain(name: str) -> bool:
    """셀렉터에 그대로 적을 수 있는 클래스명인가. 이스케이프가 필요한 것은 쓰지 않는다."""
    return all(char.isalnum() or char in "-_" for char in name)
