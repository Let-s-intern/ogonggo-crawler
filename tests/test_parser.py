"""셀렉터 적용 파서 테스트.

네트워크에 나가지 않는다. 입력은 `tests/fixtures/` 에 저장된 python.org 채용 페이지 HTML 이고,
셀렉터는 2.3.V 에서 실제 생성 호출로 얻은 것을 그대로 쓴다.
"""

from __future__ import annotations

import pathlib

import pytest

from app.crawler.parser import (
    FieldParseError,
    SelectorMissError,
    parse_detail,
    parse_list,
)
from app.selector.schema import DetailSelectors, ListSelectors

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
LIST_HTML = (FIXTURES / "pythonorg-jobs-list-20260821.html").read_text(encoding="utf-8")
DETAIL_HTML = (FIXTURES / "pythonorg-job-detail-20260821.html").read_text(encoding="utf-8")

LIST_URL = "https://www.python.org/jobs/"

LIST_SELECTORS = ListSelectors(
    item="ol.list-recent-jobs > li",
    title="span.listing-company-name > a",
    link="span.listing-company-name > a",
    date="span.listing-posted time",
)

DETAIL_SELECTORS = DetailSelectors(
    title="h1.listing-company span.company-name",
    body="div.job-description",
    qualifications="",
    recruitment_end_at="",
    department="span.listing-company-category a",
)

# 저장 시점(2026-08-21)의 픽스처가 담고 있는 값. 픽스처를 바꾸면 같이 바꾼다.
EXPECTED_ITEM_COUNT = 25
FIRST_TITLE = "Software Engineer (Remote)"
FIRST_LINK = "https://www.python.org/jobs/8126/"
FIRST_DATE = "16 August 2026"


def test_목록에서_항목_수와_필드_값이_기대값과_같다() -> None:
    result = parse_list(LIST_HTML, LIST_SELECTORS, LIST_URL)

    assert result.matched == EXPECTED_ITEM_COUNT
    assert len(result.items) == EXPECTED_ITEM_COUNT
    assert result.failures == []

    first = result.items[0]
    assert first.title == FIRST_TITLE
    assert first.date == FIRST_DATE
    # 상대경로 href 가 목록 URL 기준 절대 URL 이 된다.
    assert first.link == FIRST_LINK
    assert all(item.link.startswith("https://www.python.org/jobs/") for item in result.items)


def test_목록_텍스트를_파서가_정제하지_않는다() -> None:
    """공백·줄바꿈은 정규화가 처리한다. 파서가 미리 지우면 셀렉터가 텍스트에 묶인다."""
    dirty = ListSelectors(
        item="ol.list-recent-jobs > li",
        title="span.listing-company-name",
        link="span.listing-company-name > a",
        date="span.listing-posted",
    )
    first = parse_list(LIST_HTML, dirty, LIST_URL).items[0]

    assert first.title != first.title.strip()
    assert "\n" in first.title
    # 회사명과 "New" 배지가 섞인 원문 그대로다.
    assert "Softech Associate" in first.title
    assert first.date.startswith("Posted: ")


def test_상세에서_필드_값이_기대값과_같다() -> None:
    result = parse_detail(DETAIL_HTML, DETAIL_SELECTORS)

    assert FIRST_TITLE in result.fields["title"]
    assert "Join Softech Associate" in result.fields["body"]
    assert result.fields["department"] == "Developer / Engineer"
    # 셀렉터가 빈 값인 항목은 사이트에 없다는 응답이다. 실패가 아니다.
    assert result.fields["qualifications"] == ""
    assert result.fields["recruitment_end_at"] == ""
    assert result.missing == []


def test_item_이_0개_매칭이면_selector_miss_다() -> None:
    selectors = ListSelectors(
        item="ol.list-of-nothing > li",
        title=LIST_SELECTORS.title,
        link=LIST_SELECTORS.link,
        date=LIST_SELECTORS.date,
    )

    with pytest.raises(SelectorMissError) as caught:
        parse_list(LIST_HTML, selectors, LIST_URL)

    assert caught.value.error_class == "selector_miss"


def test_항목은_잡혔는데_필수_필드를_못_읽으면_parse_다() -> None:
    selectors = ListSelectors(
        item="ol.list-recent-jobs > li",
        title=LIST_SELECTORS.title,
        link="a.does-not-exist",
        date=LIST_SELECTORS.date,
    )

    with pytest.raises(FieldParseError) as caught:
        parse_list(LIST_HTML, selectors, LIST_URL)

    assert caught.value.error_class == "parse"


def test_못_읽은_필드만_사유에_적는다() -> None:
    """title 은 멀쩡한데 link 만 없는 사이트가 있다. 둘 다 못 읽었다고 적으면 헛짚게 된다."""
    selectors = ListSelectors(
        item="ol.list-recent-jobs > li",
        title=LIST_SELECTORS.title,
        link="a.does-not-exist",
        date=LIST_SELECTORS.date,
    )

    with pytest.raises(FieldParseError) as caught:
        parse_list(LIST_HTML, selectors, LIST_URL)

    assert "link" in str(caught.value)
    assert "title" not in str(caught.value)


def test_일부_항목만_실패하면_나머지는_남고_실패가_기록된다() -> None:
    """item 셀렉터가 공고가 아닌 영역까지 잡은 경우다. 잡힌 공고는 그대로 남는다."""
    selectors = ListSelectors(
        item="ol.list-recent-jobs > li",
        title=LIST_SELECTORS.title,
        link=LIST_SELECTORS.link,
        date="span.listing-posted time",
    )
    result = parse_list(LIST_HTML, selectors, LIST_URL)
    assert result.failures == []

    broken = ListSelectors(
        item="ol.list-recent-jobs > li, footer",
        title=LIST_SELECTORS.title,
        link=LIST_SELECTORS.link,
        date=LIST_SELECTORS.date,
    )
    partial = parse_list(LIST_HTML, broken, LIST_URL)

    assert partial.matched > len(partial.items)
    assert len(partial.items) == EXPECTED_ITEM_COUNT
    assert {failure.field for failure in partial.failures} == {"title", "link"}


def test_상세_필수_필드를_못_읽으면_parse_다() -> None:
    # 제목까지 깨뜨린다. 제목이 읽히면 본문은 제목 근처의 글로 대신 채운다
    selectors = DetailSelectors(
        title="h1.no-such-title",
        body="div.no-such-description",
        qualifications="",
        recruitment_end_at="",
        department=DETAIL_SELECTORS.department,
    )

    with pytest.raises(FieldParseError) as caught:
        parse_detail(DETAIL_HTML, selectors)

    assert "body" in str(caught.value)
    assert caught.value.error_class == "parse"


def test_선택_필드가_0개_매칭이면_실패가_아니라_missing_이다() -> None:
    selectors = DetailSelectors(
        title=DETAIL_SELECTORS.title,
        body=DETAIL_SELECTORS.body,
        qualifications="",
        recruitment_end_at="span.no-such-deadline",
        department=DETAIL_SELECTORS.department,
    )
    result = parse_detail(DETAIL_HTML, selectors)

    assert result.missing == ["recruitment_end_at"]
    assert result.fields["recruitment_end_at"] == ""


def test_셀렉터_문법_오류는_parse_다() -> None:
    selectors = ListSelectors(
        item="ol.list-recent-jobs > ",
        title=LIST_SELECTORS.title,
        link=LIST_SELECTORS.link,
        date=LIST_SELECTORS.date,
    )

    with pytest.raises(FieldParseError):
        parse_list(LIST_HTML, selectors, LIST_URL)


HANWHA_TABLE_DETAIL = """
<html><body><header><nav>메뉴 채용공고 마이페이지</nav></header>
<div class="recruit-detail"><div class="contents">
  <div class="head"><h3 class="recruit-title">[한화모멘텀] 해외영업(중국) 경력사원 채용</h3></div>
  <section class="detail-section"><h3>모집단위</h3><table><tr><td>해외영업 (중국)</td>
  <td>{duties}</td></tr></table></section>
</div></div>
<footer>서울시 중구 청계천로 86</footer></body></html>
"""


def test_본문_셀렉터가_빗나가면_제목_근처의_글을_본문으로_쓴다() -> None:
    """한화 실측(2026-09-22): 에디터형 공고로 만든 본문 셀렉터가 표로 된 공고에서 0개였다."""
    html = HANWHA_TABLE_DETAIL.format(duties="시장 조사 및 신규 거래선 발굴 " * 20)
    selectors = DetailSelectors(
        title="h3.recruit-title",
        body="div.recruit-detail-editor",
        qualifications="",
        recruitment_end_at="",
        department="",
    )

    result = parse_detail(html, selectors)

    assert "시장 조사 및 신규 거래선 발굴" in result.fields["body"]
    assert "청계천로" not in result.fields["body"]
    assert "메뉴" not in result.fields["body"]


def test_제목_근처에도_글이_모자라면_본문_실패로_남는다() -> None:
    html = HANWHA_TABLE_DETAIL.format(duties="짧다")
    selectors = DetailSelectors(
        title="h3.recruit-title",
        body="div.recruit-detail-editor",
        qualifications="",
        recruitment_end_at="",
        department="",
    )

    with pytest.raises(FieldParseError):
        parse_detail(html, selectors)


def test_본문을_대신_채우면_메모를_남긴다() -> None:
    from app.crawler.parser import FALLBACK_NOTE

    html = HANWHA_TABLE_DETAIL.format(duties="시장 조사 및 신규 거래선 발굴 " * 20)
    selectors = DetailSelectors(
        title="h3.recruit-title",
        body="div.recruit-detail-editor",
        qualifications="",
        recruitment_end_at="",
        department="",
    )

    assert parse_detail(html, selectors).notes == (FALLBACK_NOTE,)


def test_제목_근처에_공고_이미지가_있으면_본문으로_쓰고_이미지를_넘긴다() -> None:
    """한화 실측(2026-09-22): 에디터 본문이 이미지 두 장뿐이라 글이 모자랐다."""
    html = (
        '<html><body><div class="recruit-detail"><div class="head">'
        '<h3 class="recruit-title">한화솔루션 마케팅 경력</h3></div>'
        '<div class="editor"><img src="/upfile/a.png"><img src="/img/ico_docx.svg"></div>'
        "</div></body></html>"
    )
    selectors = DetailSelectors(
        title="h3.recruit-title",
        body="div.no-such-body",
        qualifications="",
        recruitment_end_at="",
        department="",
    )

    result = parse_detail(html, selectors)

    assert result.images == ("/upfile/a.png", "/img/ico_docx.svg")


def test_날짜_칸은_날짜가_든_첫_노드를_쓴다() -> None:
    """LX MMA 실측(2026-09-22): 마감일 셀렉터가 표의 값 칸 네 개를 모두 잡았다."""
    html = (
        "<html><body><h1>공고</h1><div class='body'>" + "본문 " * 10 + "</div><ul>"
        "<li><div class='label'>채용 구분</div><div class='text'>수시</div></li>"
        "<li><div class='label'>신입/경력</div><div class='text'>신입/경력</div></li>"
        "<li><div class='label'>마감일</div><div class='text'>2026.09.27 오후 11:59</div></li>"
        "</ul></body></html>"
    )
    selectors = DetailSelectors(
        title="h1",
        body="div.body",
        qualifications="",
        recruitment_end_at="li div.text",
        department="",
    )

    assert parse_detail(html, selectors).fields["recruitment_end_at"] == "2026.09.27 오후 11:59"


def test_목록이_기간을_두_줄로_나눠_적으면_이어_붙여_읽는다() -> None:
    """GS리테일 실측(2026-10-01): 시작일 줄만 읽혀 `2026.09.30 ~` 가 마감일로 들어갔다."""
    html = (
        "<html><body><ul><li class='job'><a href='/career/jobs/129853'>"
        "<p class='title'>마케팅 경력사원 채용</p>"
        "<div class='date'><p>2026.09.30 ~ </p><p>2026.10.13 23:59</p></div>"
        "</a></li></ul></body></html>"
    )
    selectors = ListSelectors(item="li.job", title="p.title", link="a", date="div.date p")

    items = parse_list(html, selectors, "https://gsretail.recruiter.co.kr/career/job").items

    assert items[0].date == "2026.09.30 ~ 2026.10.13 23:59"


def test_기간의_앞쪽만_있으면_그대로_둔다() -> None:
    html = (
        "<html><body><h1>공고</h1><div class='body'>본문</div>"
        "<p class='end'>2026.09.30 ~</p><p class='end'>마감 시 종료</p></body></html>"
    )
    selectors = DetailSelectors(
        title="h1", body="div.body", qualifications="", recruitment_end_at="p.end", department=""
    )

    assert parse_detail(html, selectors).fields["recruitment_end_at"] == "2026.09.30 ~"


def test_날짜가_든_노드가_없으면_첫_노드를_쓴다() -> None:
    html = (
        "<html><body><h1>공고</h1><div class='body'>본문</div>"
        "<p class='end'>상시채용</p></body></html>"
    )
    selectors = DetailSelectors(
        title="h1", body="div.body", qualifications="", recruitment_end_at="p.end", department=""
    )

    assert parse_detail(html, selectors).fields["recruitment_end_at"] == "상시채용"


# 마감일 라벨로 읽기 (2026-10-02, GS리테일) ------------------------------------

GS_SIDE_BOX = (
    "<html><body><h1>공고</h1><div class='JDContent_body__a1'>" + "본문 " * 10 + "</div>"
    "<ul class='JDProcess_info__8_LoR'>"
    "<li><div class='JDProcess_label__ByWrp'>채용 구분</div>"
    "<div class='JDProcess_text__x6Abn'>경력</div></li>"
    "<li><div class='JDProcess_label__ByWrp'>마감일</div>"
    "<div class='JDProcess_text__x6Abn'>2026.10.15 오후 11:59</div></li>"
    "</ul></body></html>"
)


@pytest.mark.parametrize(
    "end_selector",
    ["", "div.JDProcess_text__old99", "div.JDProcess_label__ByWrp"],
    ids=["셀렉터 없음", "클래스 해시가 바뀜", "날짜 없는 칸"],
)
def test_셀렉터가_마감일을_못_잡으면_마감일_라벨_옆_날짜를_읽는다(end_selector: str) -> None:
    """GS리테일 실측: 상세 상자의 마감일을 못 읽어 목록의 시작일(`2026.10.02 ~`)이 마감일이 됐다."""
    selectors = DetailSelectors(
        title="h1",
        body="div.JDContent_body__a1",
        qualifications="",
        recruitment_end_at=end_selector,
        department="",
    )

    parsed = parse_detail(GS_SIDE_BOX, selectors)

    assert parsed.fields["recruitment_end_at"] == "2026.10.15 오후 11:59"
    assert "recruitment_end_at" not in parsed.missing


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        (
            "<table><tr><th>접수기간</th><td>2026.08.18 ~ 2026.08.27 (10:00)</td></tr></table>",
            "2026.08.18 ~ 2026.08.27 (10:00)",
        ),
        ("<dl><dt>서류 마감</dt><dd>2026-09-05</dd></dl>", "2026-09-05"),
        (
            "<p>접수기간 : 2026년 8월 20일(목) 14:00 ~ 2026년 8월 31일(월) 23:00"
            " 접수방법 : 홈페이지</p>",
            "2026년 8월 20일(목) 14:00 ~ 2026년 8월 31일(월) 23:00",
        ),
        ("<p>서류 마감 후 2026.10.20 발표</p>", ""),
        ("<button>마감일순</button><div>2026.10.01</div>", ""),
        ("<footer><span>마감일</span><span>2026.10.01</span></footer>", ""),
        ("<div>마감일</div><div>채용시까지</div>", ""),
    ],
    ids=["표", "정의 목록", "한 칸에 라벨과 값", "본문 문장", "정렬 단추", "꼬리말", "날짜 없음"],
)
def test_마감일_라벨은_옆_칸의_날짜만_읽는다(html: str, expected: str) -> None:
    from bs4 import BeautifulSoup

    from app.crawler.parser import labeled_deadline

    assert labeled_deadline(BeautifulSoup(f"<html><body>{html}</body></html>", "html.parser")) == (
        expected
    )


def test_셀렉터가_날짜를_잡으면_라벨을_보지_않는다() -> None:
    html = GS_SIDE_BOX.replace("<h1>공고</h1>", "<h1>공고</h1><span class='end'>2026.11.30</span>")
    selectors = DetailSelectors(
        title="h1",
        body="div.JDContent_body__a1",
        qualifications="",
        recruitment_end_at="span.end",
        department="",
    )

    assert parse_detail(html, selectors).fields["recruitment_end_at"] == "2026.11.30"
