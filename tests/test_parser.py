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
    selectors = DetailSelectors(
        title=DETAIL_SELECTORS.title,
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
