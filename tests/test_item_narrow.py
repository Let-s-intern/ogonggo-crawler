"""항목 셀렉터를 제목이 있는 반복 요소로 좁히는 것 테스트.

픽스처는 2026-08-25 의 네이버 채용 목록이다. 이 페이지에서 `li.item` 은 144건 잡히고 실제
공고는 10건이다. 넓은 쪽을 저장하면 네비게이션을 공고로 세게 된다.
"""

from __future__ import annotations

import pathlib

from bs4 import BeautifulSoup

from app.selector.narrow import narrow_item_selector
from app.selector.schema import SelectorSet, validate_selectors

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
NAVER = (FIXTURES / "naver-list-20260825.html").read_text(encoding="utf-8")
WOOWA = (FIXTURES / "woowa-list-20260825.html").read_text(encoding="utf-8")


def selectors(item: str, title: str, link: str = "", date: str = "dd.info_text") -> SelectorSet:
    return validate_selectors(
        {
            "list": {"item": item, "title": title, "link": link, "date": date},
            "detail": {
                "title": "h1",
                "body": "div.body",
                "qualifications": "",
                "recruitment_end_at": "",
                "department": "",
            },
        }
    )


def matched(html: str, selector: str) -> int:
    return len(BeautifulSoup(html, "html.parser").select(selector))


def test_네비게이션까지_잡은_항목_셀렉터를_공고_10건으로_좁힌다() -> None:
    """네이버 함정. `li.item` 144건 중 제목이 있는 것은 0건이다."""
    assert matched(NAVER, "li.item") == 144

    result = narrow_item_selector(selectors("li.item", ".card_title"), NAVER)

    assert result.selectors.list.item != "li.item"
    assert matched(NAVER, result.selectors.list.item) == 10
    assert "144건" in result.note and "좁혔다" in result.note


def test_이미_공고만_잡고_있으면_그대로_둔다() -> None:
    """모델이 제대로 골랐을 때 판정이 끼어들면 운영자가 보는 것과 저장된 것이 갈린다."""
    result = narrow_item_selector(selectors("li.card_item", ".card_title"), NAVER)

    assert result.selectors.list.item == "li.card_item"
    assert result.note == ""


def test_제목_없는_항목_하나만_걸러낼_때는_원래_셀렉터를_살린다() -> None:
    """우아한형제들 목록에는 제목 없는 `li` 가 하나 섞여 있다. 항목 안의 셀렉터는 그대로 산다."""
    assert matched(WOOWA, "ul.recruit-type-list > li") == 9

    result = narrow_item_selector(
        selectors(
            "ul.recruit-type-list > li",
            "a.title p.fr-view",
            link="a.title",
            date="div.flag-type span",
        ),
        WOOWA,
    )

    assert result.selectors.list.item == "ul.recruit-type-list > li:has(a.title p.fr-view)"
    assert matched(WOOWA, result.selectors.list.item) == 8
    # 좁힌 항목 안에서 링크와 날짜가 그대로 나와야 좁힌 것이 이득이다
    soup = BeautifulSoup(WOOWA, "html.parser")
    for node in soup.select(result.selectors.list.item):
        assert node.select("a.title")
        assert node.select("div.flag-type span")


def test_제목을_빠뜨리는_후보로는_바꾸지_않는다() -> None:
    """공고 하나라도 셀렉터 밖으로 나가면 그 공고를 통째로 잃는다."""
    # 제목이 두 개 들어 있는 컨테이너를 항목으로 잡은 경우. 좁힐 후보가 없으면 그대로 둔다
    html = """
    <html><body>
      <div class="wrap"><h4 class="t">첫 공고</h4><h4 class="t">둘째 공고</h4></div>
      <div class="wrap"><span>광고</span></div>
    </body></html>
    """
    result = narrow_item_selector(selectors("div.wrap", "h4.t", date="span"), html)

    assert result.selectors.list.item == "div.wrap"
    assert result.note == ""


def test_제목_셀렉터가_비어_있으면_판정하지_않는다() -> None:
    """무엇을 기준으로 좁힐지가 없다. 추측해서 바꾸지 않는다.

    스키마는 빈 `list.title` 을 통과시키지 않으므로 모델이 채우지 못한 응답을 받는 경로
    (`parse_selectors_allowing_empty`) 와 같은 모양을 직접 만든다.
    """
    empty = selectors("li.item", ".card_title")
    empty = empty.model_copy(update={"list": empty.list.model_copy(update={"title": ""})})
    result = narrow_item_selector(empty, NAVER)

    assert result.selectors.list.item == "li.item"
    assert result.note == ""
