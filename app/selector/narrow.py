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

**매칭이 늘어나는 방향으로는 바꾸지 않는다.** 후보가 제목을 하나라도 빠뜨리면 그대로 둔다.
페이지 전체를 잡는 셀렉터는 조용히 실패하기 때문에 0개 매칭보다 나쁘다
(`.claude/agents/selector-worker.md`).

## 공고 안쪽을 잡았으면 공고 단위로 올린다

거꾸로 항목이 공고 하나 **안의** 칸들을 잡은 경우가 있다. 파수 채용(2026-10-19)은 공고마다
제목·지원하기·수정하기 링크가 같은 클래스로 있었고, 모델이 링크 칸을 항목으로 골라 공고 10건이
30건이 됐다. 이때는 매칭이 **줄어드는** 쪽으로 올린다(`_widen`).

- 잡힌 것들이 같은 개수(2개 이상)씩 서로 다른 조상 아래 묶이고, 묶음이 셋 이상이다. 공고 하나에
  칸이 몇 개씩 있는 모양이다
- 묶음 안 자리 중 하나만 글자가 묶음마다 다르고, 나머지 자리는 글자가 되풀이된다. 다른 자리가
  제목, 되풀이되는 자리가 버튼이다

항목은 묶음마다 하나씩인 가장 바깥 조상으로 올리고, 제목은 그 다른 자리를 가리키도록 조상의
클래스를 앞에 붙여 다시 고른다. 링크·날짜도 묶음마다 여럿이면 같은 접두로 좁혀 본다.

바꿨으면 무엇을 무엇으로 바꿨는지 문장으로 남긴다. 운영자가 모델이 낸 셀렉터를 보고 있다고
생각하는데 다른 것이 저장돼 있으면, 다음에 깨졌을 때 아무도 설명하지 못한다.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
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

# 공고 단위로 올릴 때 거슬러 오르는 최대 단계. 페이지 빌더는 칸 하나를 열 겹 넘게 감싼다
MAX_WIDEN_STEPS = 16
# 올리려면 묶음이 이 수 이상이어야 한다. 두셋으로는 무엇이 되풀이되는지 말할 수 없다
MIN_GROUPS = 3
# 끝이 번호인 클래스. 글마다 달라 셀렉터에 적으면 한 건만 잡힌다 (`app/selector/cleaner.py`)
_NUMBERED_CLASS = re.compile(r"[-_]\d{3,}$")


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

    refined = _refine_title(selectors, current)
    if refined is not None:
        return refined

    complete = [node for node in current if len(node.select(title)) == 1]
    if len(complete) == len(current):
        # 잡은 것이 전부 제목을 하나씩 갖고 있다. 좁힐 이유가 없다. 공고 안쪽 칸을 잡았으면 올린다
        return _widen(soup, selectors, current)

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
    usable = [
        str(name)
        for name in classes
        if str(name).strip() and _plain(str(name)) and not _NUMBERED_CLASS.search(str(name))
    ]
    return node.name + "".join(f".{name}" for name in usable)


def _plain(name: str) -> bool:
    """셀렉터에 그대로 적을 수 있는 클래스명인가. 이스케이프가 필요한 것은 쓰지 않는다."""
    return all(char.isalnum() or char in "-_" for char in name)


def _refine_title(selectors: SelectorSet, current: list[Tag]) -> Narrowing | None:
    """항목마다 제목이 똑같이 여럿씩 잡히면 항목은 두고 제목 칸을 다시 고른다.

    이때 틀린 것은 항목이 아니라 제목 셀렉터다. 파수 채용(2026-10-19)에서 모델이 항목을
    `article.elementor-post` 로 맞게 골랐는데 제목 셀렉터가 제목·지원하기·수정하기 셋을 다 잡았고,
    좁히기가 이것을 공고 하나에 제목이 셋이라고 읽어 링크 칸(30건)으로 내렸다. 묶음 안에서 글자가
    공고마다 다른 칸을 못 찾으면 `None` 이고, 부르는 쪽은 좁히지 않고 그대로 둔다.
    """
    title = selectors.list.title.strip()
    counts = {len(node.select(title)) for node in current}
    if len(current) < MIN_GROUPS or len(counts) != 1 or counts.pop() < 2:
        return None
    groups = [(node, list(node.select(title))) for node in current]
    position = _title_position(groups)
    if position is None:
        # 무엇이 제목인지 모른다. 항목을 좁히면 공고를 칸으로 쪼개므로 그대로 둔다
        return Narrowing(selectors)
    picked = [members[position] for _, members in groups]
    new_title = _scoped(current, picked, title)
    if not new_title:
        return Narrowing(selectors)
    update = {"title": new_title}
    for name in INNER_FIELDS:
        value = getattr(selectors.list, name).strip()
        if value:
            update[name] = _scoped(current, picked, value) or value
    logger.info("제목 셀렉터를 다시 고른다 %s -> %s", title, new_title)
    return Narrowing(
        selectors=selectors.model_copy(update={"list": selectors.list.model_copy(update=update)}),
        note=(
            f"제목 셀렉터 `{title}` 이 항목마다 칸 {len(groups[0][1])}개를 잡아, 공고마다 글자가 "
            f"다른 칸 `{new_title}` 로 다시 골랐다. 항목 `{selectors.list.item}` 은 그대로다"
        ),
    )


def _widen(soup: BeautifulSoup, selectors: SelectorSet, current: list[Tag]) -> Narrowing:
    """항목이 공고 하나 안의 칸들을 잡았으면 공고 단위로 올린다. 아니면 그대로 둔다."""
    item = selectors.list.item.strip()
    title = selectors.list.title.strip()
    grouping = _grouping(current)
    if grouping is None:
        return Narrowing(selectors)
    groups, position = grouping
    picked = [members[position] for _, members in groups]

    candidate = _group_selector(soup, groups)
    if not candidate:
        return Narrowing(selectors)
    anchors = soup.select(candidate)
    new_title = _scoped(anchors, picked, title)
    if not new_title:
        return Narrowing(selectors)

    update = {"item": candidate, "title": new_title}
    for name in INNER_FIELDS:
        value = getattr(selectors.list, name).strip()
        if value:
            update[name] = _scoped(anchors, picked, value) or value
    widened = selectors.list.model_copy(update=update)
    logger.info(
        "항목 셀렉터를 올린다 %s(%d건) -> %s(%d건)", item, len(current), candidate, len(anchors)
    )
    return Narrowing(
        selectors=selectors.model_copy(update={"list": widened}),
        note=(
            f"항목 셀렉터 `{item}` 이 공고 하나 안의 칸 {len(current) // len(groups)}개씩, "
            f"{len(current)}건을 잡아 공고 단위 `{candidate}`({len(anchors)}건)로 올렸다. "
            f"제목은 묶음마다 글자가 다른 칸 `{new_title}` 로 골랐다"
        ),
    )


def _grouping(current: list[Tag]) -> tuple[list[tuple[Tag, list[Tag]]], int] | None:
    """(묶음 조상과 그 안의 항목들, 제목 자리). 공고 안쪽 칸을 잡은 모양이 아니면 `None`.

    가장 낮은 단계에서 고르게 묶이는 것을 찾는다. 위로 갈수록 묶음이 커지다 결국 하나가 된다.
    """
    for step in range(1, MAX_WIDEN_STEPS + 1):
        ancestors = [_ancestor(node, step) for node in current]
        if any(node is None for node in ancestors):
            return None
        groups: dict[int, tuple[Tag, list[Tag]]] = {}
        for node, ancestor in zip(current, ancestors, strict=True):
            assert ancestor is not None
            groups.setdefault(id(ancestor), (ancestor, []))[1].append(node)
        if len(groups) == len(current):
            continue
        listed = list(groups.values())
        if len(listed) < MIN_GROUPS:
            return None
        if len({len(members) for _, members in listed}) != 1:
            # 공고 안에서 줄이 갈려 칸이 고르지 않게 묶였다(파수는 두 줄에 2개·1개). 더 올라간다
            continue
        position = _title_position(listed)
        return (listed, position) if position is not None else None
    return None


def _title_position(groups: list[tuple[Tag, list[Tag]]]) -> int | None:
    """묶음 안 자리 중 글자가 묶음마다 다른 단 하나의 자리. 나머지 자리는 글자가 되풀이돼야 한다."""
    size = len(groups[0][1])
    distinct: list[int] = []
    for position in range(size):
        texts = [members[position].get_text(" ", strip=True) for _, members in groups]
        if not all(texts):
            continue
        top = Counter(texts).most_common(1)[0][1]
        if top == 1:
            distinct.append(position)
        elif top * 2 < len(texts):
            # 되풀이도 아니고 다 다르지도 않다. 무엇이 제목인지 말할 수 없다
            return None
    return distinct[0] if len(distinct) == 1 else None


def _group_selector(soup: BeautifulSoup, groups: list[tuple[Tag, list[Tag]]]) -> str:
    """묶음마다 하나씩인 가장 바깥 조상의 셀렉터. 못 찾으면 빈 문자열."""
    first = groups[0][0]
    chain: list[Tag] = []
    node: Tag | None = first
    for _ in range(MAX_WIDEN_STEPS):
        if node is None or node.name in ("[document]", "body", "html"):
            break
        chain.append(node)
        parent = node.parent
        node = parent if isinstance(parent, Tag) else None

    wanted = len(groups)
    members = [members for _, members in groups]
    best = ""
    for ancestor in chain:
        for selector in _candidates(ancestor):
            try:
                matched = soup.select(selector)
            except SelectorSyntaxError:
                continue
            # 묶음마다 하나씩, 빠짐없이 잡아야 한다
            if len(matched) != wanted:
                continue
            if all(
                sum(1 for one in group if _contains(anchor, one)) == len(group)
                for anchor, group in zip(matched, members, strict=True)
            ):
                best = selector
                break
    return best


def _scoped(anchors: list[Tag], picked: list[Tag], selector: str) -> str:
    """묶음마다 `picked` 하나만 잡히도록 `selector` 앞에 그 칸 조상의 클래스를 붙인다.

    이미 묶음마다 하나씩 잡히면 그대로, 접두를 붙여도 안 되면 빈 문자열이다.
    """
    pairs = list(zip(anchors, picked, strict=True))
    if _one_each(pairs, selector):
        return selector
    node = picked[0]
    for _ in range(MAX_WIDEN_STEPS):
        parent = node.parent
        if not isinstance(parent, Tag) or parent is anchors[0]:
            break
        node = parent
        own = _own(node)
        if own == node.name:
            continue
        # 셀렉터가 `td a ...` 처럼 조상부터 적혀 있으면 앞에 붙인 조상과 겹친다. 마지막 마디만
        # 붙인 것도 본다
        for scoped in (f"{own} {selector}", f"{own} {selector.split()[-1]}"):
            if _one_each(pairs, scoped):
                return scoped
    return ""


def _one_each(pairs: list[tuple[Tag, Tag]], selector: str) -> bool:
    """묶음마다 `selector` 가 하나만 잡히고, 그것이 고른 칸이거나 그 칸 안에 있는가."""
    try:
        for anchor, chosen in pairs:
            matched = anchor.select(selector)
            if len(matched) != 1:
                return False
            hit = matched[0]
            if hit is not chosen and not _contains(chosen, hit) and not _contains(hit, chosen):
                return False
    except SelectorSyntaxError:
        return False
    return True


def _contains(outer: Tag, inner: Tag) -> bool:
    return any(parent is outer for parent in inner.parents)
