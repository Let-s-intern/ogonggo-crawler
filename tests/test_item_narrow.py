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


# 공고마다 제목·지원하기·수정하기 링크가 같은 클래스로 있고, 두 줄에 나뉘어 있다. 글마다 번호
# 클래스가 붙는다. 파수 채용이 이 모양이다 (2026-10-19)
BUTTON_ROWS = (
    "<html><body><div class='posts'>"
    + "".join(
        f'<article class="post post-{15000 + n}"><table><tbody>'
        f'<tr><td class="cell kind">경력</td>'
        f'<td class="cell c-title"><a class="link" href="/apply/{n}">'
        f'<div class="content"><div class="text">공고 {n}</div></div></a></td>'
        f'<td class="cell c-date">2026.10.{n + 10}</td>'
        f'<td class="cell c-apply"><a class="link" href="/form?idx={n}">'
        '<div class="content"><div class="text">지원하기</div></div></a></td></tr>'
        f'<tr><td class="cell c-edit"><a class="link" href="/edit?idx={n}">'
        '<div class="content"><div class="text">수정하기</div></div></a></td></tr>'
        "</tbody></table></article>"
        for n in range(5)
    )
    + "</div></body></html>"
)


def test_공고_안의_링크_칸을_잡았으면_공고_단위로_올리고_제목_칸을_다시_고른다() -> None:
    narrowing = narrow_item_selector(
        selectors("a.link > div.content", "a.link .text", link="a.link", date=""), BUTTON_ROWS
    )

    found = narrowing.selectors.list
    assert found.item == "article.post"
    soup = BeautifulSoup(BUTTON_ROWS, "html.parser")
    items = soup.select(found.item)
    assert [item.select_one(found.title).get_text(strip=True) for item in items] == [  # type: ignore[union-attr]
        f"공고 {n}" for n in range(5)
    ]
    assert [item.select_one(found.link)["href"] for item in items] == [  # type: ignore[index]
        f"/apply/{n}" for n in range(5)
    ]
    assert "공고 단위 `article.post`(5건)로 올렸다" in narrowing.note


def test_조상부터_적은_제목_셀렉터도_공고_단위로_올린다() -> None:
    narrowing = narrow_item_selector(
        selectors("a.link > div.content", "td a.link .text", link="td a.link", date=""), BUTTON_ROWS
    )

    found = narrowing.selectors.list
    assert found.item == "article.post"
    soup = BeautifulSoup(BUTTON_ROWS, "html.parser")
    titles = [item.select_one(found.title) for item in soup.select(found.item)]
    assert [title.get_text(strip=True) for title in titles if title] == [
        f"공고 {n}" for n in range(5)
    ]


def test_묶음_안_칸이_모두_제목처럼_달라도_올리지_않는다() -> None:
    """무엇이 제목인지 말할 수 없으면 모델이 낸 것을 그대로 둔다."""
    html = (
        "<html><body>"
        + "".join(
            f'<div class="card"><a class="link" href="/a/{n}">가 {n}</a>'
            f'<a class="link" href="/b/{n}">나 {n}</a></div>'
            for n in range(5)
        )
        + "</body></html>"
    )

    narrowing = narrow_item_selector(selectors("a.link", "a.link", date=""), html)

    assert narrowing.selectors.list.item == "a.link"
    assert narrowing.note == ""


def test_목록의_공고가_한_줄씩이면_올리지_않는다() -> None:
    html = (
        "<html><body><ul>"
        + "".join(
            f'<li class="row"><a class="link" href="/a/{n}">공고 {n}</a></li>' for n in range(5)
        )
        + "</ul></body></html>"
    )

    narrowing = narrow_item_selector(selectors("li.row", "a.link", date=""), html)

    assert narrowing.selectors.list.item == "li.row"
    assert narrowing.note == ""


def test_항목은_맞고_제목이_버튼까지_잡으면_항목을_좁히지_않고_제목만_다시_고른다() -> None:
    """파수 실측(2026-10-19): 맞게 고른 `article` 을 좁히기가 링크 칸 30건으로 내렸다."""
    narrowing = narrow_item_selector(
        selectors("article.post", "a.link .text", link="a.link", date=""), BUTTON_ROWS
    )

    found = narrowing.selectors.list
    assert found.item == "article.post"
    soup = BeautifulSoup(BUTTON_ROWS, "html.parser")
    titles = [item.select_one(found.title) for item in soup.select(found.item)]
    assert [title.get_text(strip=True) for title in titles if title] == [
        f"공고 {n}" for n in range(5)
    ]
    assert "항목 `article.post` 은 그대로다" in narrowing.note
