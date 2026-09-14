"""`list.link` 판정 테스트.

픽스처 셋을 쓴다. 셋 다 네트워크에 나가지 않는다.

- `hanwha-list-items-20260822.html`: 2026-08-22 에 렌더한 한화 목록에서 항목 3개만 남긴 것.
  항목 안에 `a` 가 하나도 없다
- `hyundai-list-items-20260822.html`: 같은 날 렌더한 현대자동차 목록에서 항목 3개만 남긴 것.
  `a` 는 있지만 `href` 가 전부 `javascript:` 다
- `pythonorg-jobs-list-20260821.html`: 정상 `href` 가 있는 목록

노드 수만 세던 때는 앞의 둘도 통과했다. 그것이 이 파일이 막는 것이다.
"""

from __future__ import annotations

import pathlib

import pytest
from bs4 import BeautifulSoup

from app.crawler.parser import FieldParseError, parse_list
from app.selector.link import followable, resolve_link
from app.selector.schema import ListSelectors, validate_selectors
from app.selector.verify import verify_selectors

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
HANWHA_HTML = (FIXTURES / "hanwha-list-items-20260822.html").read_text(encoding="utf-8")
HYUNDAI_HTML = (FIXTURES / "hyundai-list-items-20260822.html").read_text(encoding="utf-8")
PYTHONORG_HTML = (FIXTURES / "pythonorg-jobs-list-20260821.html").read_text(encoding="utf-8")

# 생성 모델이 실제로 골랐던 셀렉터. `h4.recruit-title` 은 링크가 아니다
HANWHA_LIST = ListSelectors(
    item=".recruit-list > li",
    title=".recruit-title",
    link=".recruit-title",
    date=".recruit-terms .terms",
    company_name=".affiliate-name",
)

HYUNDAI_LIST = ListSelectors(
    item="#applyList .apply__list > li",
    title=".top strong",
    link="a",
    date=".d__day",
)

PYTHONORG_LIST = ListSelectors(
    item="ol.list-recent-jobs > li",
    title="span.listing-company-name > a",
    link="span.listing-company-name > a",
    date="span.listing-posted time",
)

DETAIL = {
    "title": "h1",
    "body": "div",
    "qualifications": "",
    "recruitment_end_at": "",
    "department": "",
}


def items(html: str, selectors: ListSelectors) -> list:
    return BeautifulSoup(html, "html.parser").select(selectors.item)


def report_for(html: str, selectors: ListSelectors) -> dict:
    """목록 셀렉터만 픽스처에 돌린다. 상세 판정은 이 테스트의 관심이 아니다."""
    full = validate_selectors({"list": selectors.model_dump(), "detail": DETAIL})
    result = verify_selectors(full, html, "<html><h1>t</h1><div>b</div></html>")
    return {field.name: field for field in result.fields}


def test_an_element_without_an_anchor_fails_even_with_every_node_matched() -> None:
    """한화: 항목 3건을 다 잡지만 링크는 0건이다."""
    nodes = items(HANWHA_HTML, HANWHA_LIST)
    assert len(nodes) == 3
    assert all(not node.select("a") for node in nodes)

    fields = report_for(HANWHA_HTML, HANWHA_LIST)
    assert fields["list.item"].matches == 3
    assert fields["list.title"].matches == 3
    assert fields["list.link"].matches == 0
    assert fields["list.link"].status == "failed"
    assert "href 가 없다" in fields["list.link"].message


def test_a_javascript_href_fails() -> None:
    """현대자동차: `a` 는 잡히지만 `href` 가 `javascript:` 라 따라갈 수 없다."""
    nodes = items(HYUNDAI_HTML, HYUNDAI_LIST)
    assert len(nodes) == 3
    assert all(node.select("a") for node in nodes)

    fields = report_for(HYUNDAI_HTML, HYUNDAI_LIST)
    assert fields["list.item"].matches == 3
    assert fields["list.link"].matches == 0
    assert fields["list.link"].status == "failed"
    assert "javascript:" in fields["list.link"].message


def test_a_real_href_is_a_success() -> None:
    fields = report_for(PYTHONORG_HTML, PYTHONORG_LIST)
    assert fields["list.link"].status == "ok"
    assert fields["list.link"].matches == fields["list.item"].matches


def test_the_failed_field_is_named_in_the_report() -> None:
    full = validate_selectors({"list": HANWHA_LIST.model_dump(), "detail": DETAIL})
    result = verify_selectors(full, HANWHA_HTML, "<html><h1>t</h1><div>b</div></html>")

    assert "list.link" in result.failed
    assert result.ok is False
    # 목록 전체가 실패한 것은 아니다. 링크만 못 뽑는 것이지 목록은 읽힌다
    assert result.list_missing is False


def test_resolve_link_says_why_it_failed() -> None:
    hanwha = resolve_link(items(HANWHA_HTML, HANWHA_LIST)[0], HANWHA_LIST)
    assert hanwha.ok is False
    assert hanwha.url == ""
    assert "href 가 없다" in hanwha.reason

    hyundai = resolve_link(items(HYUNDAI_HTML, HYUNDAI_LIST)[0], HYUNDAI_LIST)
    assert hyundai.ok is False
    assert "javascript:" in hyundai.reason

    ok = resolve_link(items(PYTHONORG_HTML, PYTHONORG_LIST)[0], PYTHONORG_LIST)
    assert ok.ok is True
    assert ok.url.startswith("/jobs/")


def test_only_http_and_a_relative_path_are_followable() -> None:
    assert followable("https://talent.hyundai.com/apply/applyView.hc?recuYy=2026") is True
    assert followable("/jobs/8126/") is True
    assert followable("javascript:void(0)") is False
    assert followable("javascript:;") is False
    assert followable("#") is False
    assert followable("mailto:recruit@example.com") is False
    assert followable("") is False


# 14.2: 속성 + URL 템플릿 방식
HYUNDAI_TEMPLATE = ListSelectors(
    item="#applyList .apply__list > li",
    title=".top strong",
    # 링크가 될 a 가 없다. 속성은 항목 노드 자신에 있으므로 셀렉터를 비운다
    link="",
    date=".d__day",
    link_template=(
        "https://talent.hyundai.com/apply/applyView.hc"
        "?recuYy={data-recuyy}&recuType={data-recutype}&recuCls={data-recucls}"
    ),
)

# 픽스처가 담고 있는 앞의 세 항목. 픽스처를 바꾸면 같이 바꾼다
EXPECTED_HYUNDAI_URLS = [
    "https://talent.hyundai.com/apply/applyView.hc?recuYy=2026&recuType=N2&recuCls=296",
    "https://talent.hyundai.com/apply/applyView.hc?recuYy=2026&recuType=N2&recuCls=295",
    "https://talent.hyundai.com/apply/applyView.hc?recuYy=2026&recuType=N2&recuCls=312",
]

HYUNDAI_LIST_URL = "https://talent.hyundai.com/theme/hall.hc"


def test_a_template_assembles_the_url_from_three_attributes() -> None:
    result = parse_list(HYUNDAI_HTML, HYUNDAI_TEMPLATE, HYUNDAI_LIST_URL)

    assert result.matched == 3
    assert [item.link for item in result.items] == EXPECTED_HYUNDAI_URLS
    assert result.failures == []
    # 조립한 URL 이 공용 fetch 클라이언트를 통과할 모양인지
    assert all(followable(item.link) for item in result.items)


def test_the_template_is_also_a_success_in_verification() -> None:
    fields = report_for(HYUNDAI_HTML, HYUNDAI_TEMPLATE)

    assert fields["list.link"].status == "ok"
    assert fields["list.link"].matches == 3
    assert fields["list.link"].selector == HYUNDAI_TEMPLATE.link_template


def test_an_item_missing_the_attribute_is_recorded_as_a_failure() -> None:
    """속성이 없는 항목은 URL 을 지어내지 않고 실패로 남긴다."""
    broken = HYUNDAI_HTML.replace('data-recucls="295"', "", 1)

    result = parse_list(broken, HYUNDAI_TEMPLATE, HYUNDAI_LIST_URL)

    assert [item.link for item in result.items] == [
        EXPECTED_HYUNDAI_URLS[0],
        EXPECTED_HYUNDAI_URLS[2],
    ]
    assert [(f.index, f.field) for f in result.failures] == [(1, "link")]
    assert "data-recucls" in result.failures[0].message


def test_an_item_with_no_attribute_at_all_fails_with_the_reason() -> None:
    """한화: 상세 파라미터가 DOM 에 없어서 템플릿 방식으로도 풀리지 않는다."""
    hanwha_template = ListSelectors(
        item=".recruit-list > li",
        title=".recruit-title",
        link="",
        date=".recruit-terms .terms",
        link_template="https://www.hanwhain.com/portal/apply/recruit/detail?rtSeq={data-rt-seq}",
    )

    with pytest.raises(FieldParseError) as caught:
        parse_list(HANWHA_HTML, hanwha_template, "https://www.hanwhain.com/portal/apply/recruit")

    assert "data-rt-seq" in str(caught.value)
    assert "link" in str(caught.value)


def test_a_template_that_is_not_http_is_refused() -> None:
    selectors = ListSelectors(
        item="#applyList .apply__list > li",
        title=".top strong",
        link="",
        date=".d__day",
        link_template="javascript:goView({data-recucls})",
    )

    resolved = resolve_link(items(HYUNDAI_HTML, selectors)[0], selectors)

    assert resolved.ok is False
    assert "http(s) 가 아니다" in resolved.reason


def test_selectors_saved_before_the_template_existed_still_read_href() -> None:
    """`link_template` 키가 없는 기존 셀렉터 JSON 이 그대로 동작한다."""
    stored = {
        "list": {
            "item": "ol.list-recent-jobs > li",
            "title": "span.listing-company-name > a",
            "link": "span.listing-company-name > a",
            "date": "span.listing-posted time",
        },
        "detail": DETAIL,
    }

    selectors = validate_selectors(stored)
    assert selectors.list.link_template == ""

    result = parse_list(PYTHONORG_HTML, selectors.list, "https://www.python.org/jobs/")
    assert result.matched > 1
    assert result.items[0].link.startswith("https://www.python.org/jobs/")


def test_onclick_인자를_자리표시자로_읽는다() -> None:
    """두산·네이버는 공고 번호가 속성값 전체가 아니라 `onclick` 인자에 들어 있다."""
    node = items(
        '<li><a class="tit" href="javascript:void(0);"'
        " onclick=\"goDetail('1000361539', 'C_REC_MGT_04')\">제목</a></li>",
        ListSelectors(item="li", title="a", link="a.tit", date=""),
    )[0]
    selectors = ListSelectors(
        item="li",
        title="a",
        link="a.tit",
        date="",
        link_template=(
            "https://career.doosan.com/dsp/sa/RecList.jsp"
            "?REC_ID={onclick|arg1}&REC_MGT_CD={onclick|arg2}&mode=goDetail"
        ),
    )

    result = resolve_link(node, selectors)

    assert result.ok is True
    assert result.url == (
        "https://career.doosan.com/dsp/sa/RecList.jsp"
        "?REC_ID=1000361539&REC_MGT_CD=C_REC_MGT_04&mode=goDetail"
    )


def test_없는_인자를_요구하면_그_항목은_실패다() -> None:
    """반쯤 채워진 주소를 만들지 않는다. 그것으로 요청하면 엉뚱한 공고를 가져온다."""
    node = items(
        '<li><a class="tit" onclick="show(\'30005276\')">제목</a></li>',
        ListSelectors(item="li", title="a", link="a.tit", date=""),
    )[0]
    selectors = ListSelectors(
        item="li",
        title="a",
        link="a.tit",
        date="",
        link_template="https://example.test/view?id={onclick|arg2}",
    )

    result = resolve_link(node, selectors)

    assert result.ok is False
    assert "onclick|arg2" in result.reason
