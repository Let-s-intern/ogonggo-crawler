"""생성된 셀렉터를 방금 가져온 그 HTML 에 즉시 적용해 본다.

`.claude/rules/llm.md`: 0개 매칭 필드는 성공으로 내놓지 않는다. 실패한 필드 이름을 그대로
결과에 넣어 운영자가 어디를 고쳐야 하는지 바로 알게 한다.

목록 필드가 하나도 안 잡히는 것은 필드 하나가 틀린 것과 다르다. 그 페이지의 정적 HTML 에
목록 자체가 없다는 뜻이고, 손으로 고칠 셀렉터가 없다. `list_missing` 이 그 경우를 따로 알려
호출부가 부분 실패와 다르게 다루게 한다.

여기서 하는 것은 "그 셀렉터가 노드를 잡는가"까지다. 잡은 값이 깨끗한지는 정규화의 몫이고,
공백이나 광고 문구가 섞였다고 셀렉터를 바꾸지 않는다.

`list.link` 만 예외다. 이 필드는 노드 수가 아니라 **따라갈 수 있는 URL 이 나오는지**로
판정한다. 노드 수만 세면 `href` 가 없는 요소를 골라도 통과하고, 통과한 셀렉터로 실행하면
링크가 없어 실패한다. 판정은 파서와 같은 `app/selector/link.py` 를 쓴다.
"""

from __future__ import annotations

from dataclasses import dataclass

from bs4 import BeautifulSoup
from bs4.element import Tag
from soupsieve import SelectorSyntaxError

from app.selector.link import resolve_link
from app.selector.schema import (
    DETAIL_FIELDS,
    LIST_SELECTOR_FIELDS,
    OPTIONAL_DETAIL_FIELDS,
    OPTIONAL_LIST_FIELDS,
    ListSelectors,
    SelectorSet,
)

# 필드 하나의 판정.
OK = "ok"
FAILED = "failed"
SKIPPED = "skipped"


@dataclass(frozen=True)
class FieldMatch:
    """`list.title` 처럼 구역까지 붙인 이름으로 남긴다."""

    name: str
    selector: str
    matches: int
    status: str
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == OK


@dataclass(frozen=True)
class VerificationReport:
    fields: list[FieldMatch]

    @property
    def failed(self) -> list[str]:
        """실패한 필드 이름. 비어 있어야 생성이 성공이다."""
        return [field.name for field in self.fields if field.status == FAILED]

    @property
    def ok(self) -> bool:
        return not self.failed

    @property
    def skipped(self) -> list[str]:
        """셀렉터가 비어 판정하지 않은 필드 이름.

        매칭이 0개지만 실패가 아니다 — 모델이 "사이트에 그 항목이 없다"고 답한 것이라 고칠
        셀렉터가 없다. 이 목록을 화면에 그대로 넘겨야 운영자가 못 뽑은 것과 원래 없는 것을
        가른다. 성공으로 섞어 적으면 둘이 같은 줄로 보인다.
        """
        return [field.name for field in self.fields if field.status == SKIPPED]

    @property
    def failed_list_fields(self) -> list[str]:
        """실패한 목록 필드 이름만."""
        return [
            field.name
            for field in self.fields
            if field.name.startswith("list.") and field.status == FAILED
        ]

    @property
    def list_missing(self) -> bool:
        """목록 필드가 전부 0개 매칭인가.

        참이면 정적 HTML 에 목록이 없는 것이지 셀렉터 하나가 틀린 것이 아니다. 응답이 없다고
        한 선택 필드(`skipped`)는 판정에서 뺀다 — 셀렉터가 없으니 실패도 성공도 아니다.
        """
        checked = [
            field
            for field in self.fields
            if field.name.startswith("list.") and field.status != SKIPPED
        ]
        return bool(checked) and all(field.status == FAILED for field in checked)

    @property
    def list_fields_missing(self) -> bool:
        """목록 항목은 잡혔는데 그 안에서 아무 필드도 뽑지 못했는가.

        참이면 `item` 이 목록이 아닌 다른 반복 요소를 잡았거나 항목 안의 셀렉터가 실제
        구조와 다르다. 어느 쪽이든 제목도 링크도 날짜도 없는 공고만 나오는 크롤러라 못 쓴다.

        항목 개수로는 판정하지 않는다 — 공고가 진짜 1건인 목록 페이지가 있고, 그것은 정상이다.
        판정에 쓰는 것은 항목 안의 `title`·`link`·`date` 뿐이고, 셀렉터가 없어 건너뛴 것은
        뺀다. `company_name` 는 넣지 않는다. 계열사 이름은 있어도 그만인 정보라 그것 하나가 잡혔다고
        쓸 수 있는 크롤러가 되지 않는다.
        """
        by_name = {field.name: field for field in self.fields}
        item = by_name.get("list.item")
        if item is None or item.status != OK:
            # 항목 자체가 없는 것은 다른 실패다. `list_missing` 이 그 경우를 맡는다
            return False
        checked = [
            by_name[name]
            for name in ("list.title", "list.link", "list.date")
            if name in by_name and by_name[name].status != SKIPPED
        ]
        return bool(checked) and all(field.status == FAILED for field in checked)

    def summary(self) -> dict[str, int]:
        """필드 이름 -> 매칭 개수. 화면과 로그가 그대로 쓴다."""
        return {field.name: field.matches for field in self.fields}


def verify_selectors(
    selectors: SelectorSet, list_html: str, detail_html: str
) -> VerificationReport:
    """목록·상세 셀렉터를 각각의 HTML 에 적용해 매칭 개수를 센다."""
    return VerificationReport(
        fields=_verify_list(selectors, list_html) + _verify_detail(selectors, detail_html)
    )


def _verify_list(selectors: SelectorSet, html: str) -> list[FieldMatch]:
    soup = BeautifulSoup(html, "html.parser")
    item_selector = selectors.list.item

    try:
        items = soup.select(item_selector)
    except SelectorSyntaxError as exc:
        broken = _syntax_error("list.item", item_selector, exc)
        return [broken] + [
            FieldMatch(
                name=f"list.{name}",
                selector=getattr(selectors.list, name),
                matches=0,
                status=FAILED,
                message="list.item 이 깨져 확인할 수 없다",
            )
            for name in LIST_SELECTOR_FIELDS
            if name != "item"
        ]

    results = [
        FieldMatch(
            name="list.item",
            selector=item_selector,
            matches=len(items),
            status=OK if items else FAILED,
            message="" if items else "매칭 0개. 사이트 구조가 다르거나 JS 렌더링이다",
        )
    ]

    for name in LIST_SELECTOR_FIELDS:
        if name == "item":
            continue
        if name == "link":
            # 노드 수가 아니라 따라갈 수 있는 URL 이 나오는지로 본다
            results.append(_verify_link(selectors.list, items))
            continue
        selector = getattr(selectors.list, name)
        if not selector and name in OPTIONAL_LIST_FIELDS:
            # 목록에 그 항목이 없다는 응답이다. 셀렉터가 없으니 실패도 성공도 아니다.
            results.append(
                FieldMatch(
                    name=f"list.{name}",
                    selector="",
                    matches=0,
                    status=SKIPPED,
                    message="목록에 해당 항목이 없다는 응답",
                )
            )
            continue
        try:
            matched = sum(1 for item in items if item.select(selector))
        except SelectorSyntaxError as exc:
            results.append(_syntax_error(f"list.{name}", selector, exc))
            continue
        results.append(
            FieldMatch(
                name=f"list.{name}",
                selector=selector,
                matches=matched,
                status=OK if matched else FAILED,
                message="" if matched else f"항목 {len(items)}건 중 어디에도 없다",
            )
        )
    return results


def _verify_link(selectors: ListSelectors, items: list[Tag]) -> FieldMatch:
    """`list.link` 판정. 매칭 개수는 상세로 따라갈 수 있는 항목의 수다.

    한 건도 안 나오면 첫 항목의 사유를 그대로 붙인다. 운영자가 `href` 가 없는 것인지
    `javascript:` 인 것인지를 보고 바로 다음 수단을 고를 수 있어야 한다.
    """
    try:
        resolved = [resolve_link(item, selectors) for item in items]
    except SelectorSyntaxError as exc:
        return _syntax_error("list.link", selectors.link, exc)

    usable = sum(1 for result in resolved if result.ok)
    reasons = [result.reason for result in resolved if not result.ok]
    message = ""
    if not usable:
        message = f"항목 {len(items)}건 중 따라갈 수 있는 상세 URL 이 0건이다"
        if reasons:
            message = f"{message}: {reasons[0]}"
    return FieldMatch(
        name="list.link",
        # 속성 + 템플릿 방식은 셀렉터가 비어 있을 수 있다. 그때는 템플릿을 보여 준다
        selector=selectors.link or selectors.link_template,
        matches=usable,
        status=OK if usable else FAILED,
        message=message,
    )


def _verify_detail(selectors: SelectorSet, html: str) -> list[FieldMatch]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[FieldMatch] = []

    for name in DETAIL_FIELDS:
        selector = getattr(selectors.detail, name)
        full_name = f"detail.{name}"

        if not selector and name in OPTIONAL_DETAIL_FIELDS:
            # 사이트에 그 항목이 없다는 응답이다. 셀렉터가 없으니 실패도 성공도 아니다.
            results.append(
                FieldMatch(
                    name=full_name,
                    selector="",
                    matches=0,
                    status=SKIPPED,
                    message="페이지에 해당 항목이 없다는 응답",
                )
            )
            continue

        try:
            matched = len(soup.select(selector))
        except SelectorSyntaxError as exc:
            results.append(_syntax_error(full_name, selector, exc))
            continue
        results.append(
            FieldMatch(
                name=full_name,
                selector=selector,
                matches=matched,
                status=OK if matched else FAILED,
                message="" if matched else "매칭 0개",
            )
        )
    return results


def _syntax_error(name: str, selector: str, exc: SelectorSyntaxError) -> FieldMatch:
    return FieldMatch(
        name=name,
        selector=selector,
        matches=0,
        status=FAILED,
        message=f"셀렉터 문법 오류: {exc}",
    )
