"""HTML 정제 테스트. 저장된 픽스처만 본다. 네트워크에 나가지 않는다."""

from __future__ import annotations

import pathlib

import pytest
from bs4 import BeautifulSoup, Comment

from app.selector.cleaner import DEFAULT_MAX_CHARS, clean_html

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
LIST_FIXTURE = FIXTURES / "pythonorg-jobs-list-20260821.html"
DETAIL_FIXTURE = FIXTURES / "pythonorg-job-detail-20260821.html"


@pytest.fixture
def list_html() -> str:
    return LIST_FIXTURE.read_text(encoding="utf-8")


@pytest.fixture
def detail_html() -> str:
    return DETAIL_FIXTURE.read_text(encoding="utf-8")


def test_fixture_has_the_noise_we_claim_to_remove(list_html: str) -> None:
    """정제 전 픽스처에 script 와 주석이 실제로 들어 있어야 검증이 의미가 있다."""
    soup = BeautifulSoup(list_html, "html.parser")

    assert soup.find_all("script")
    assert soup.find_all(string=lambda node: isinstance(node, Comment))


def test_removes_script_style_svg_and_comments(list_html: str) -> None:
    cleaned = clean_html(list_html)
    soup = BeautifulSoup(cleaned.html, "html.parser")

    assert soup.find_all("script") == []
    assert soup.find_all("style") == []
    assert soup.find_all("svg") == []
    assert soup.find_all(string=lambda node: isinstance(node, Comment)) == []


def test_removes_inline_event_handlers() -> None:
    html = '<div><a href="/x" onclick="steal()" onmouseover="x()">공고</a></div>'

    cleaned = clean_html(html)

    assert "onclick" not in cleaned.html
    assert "onmouseover" not in cleaned.html
    assert 'href="/x"' in cleaned.html


def test_repeating_list_is_sampled_to_four_items(list_html: str) -> None:
    """공고 목록은 형제 4개만 남는다. 60개나 4개나 구조 신호는 같다."""
    before = BeautifulSoup(list_html, "html.parser").select("ol.list-recent-jobs > li")
    cleaned = clean_html(list_html)
    after = BeautifulSoup(cleaned.html, "html.parser").select("ol.list-recent-jobs > li")

    assert len(before) > 4
    assert len(after) <= 4
    assert cleaned.removed_siblings >= len(before) - 4


def test_sampled_item_keeps_its_fields(list_html: str) -> None:
    """남은 항목에는 셀렉터 생성에 필요한 필드가 그대로 있어야 한다."""
    cleaned = clean_html(list_html)
    item = BeautifulSoup(cleaned.html, "html.parser").select_one("ol.list-recent-jobs > li")

    assert item is not None
    assert item.select_one("h2.listing-company a") is not None
    assert item.select_one("span.listing-posted time") is not None


def test_output_is_within_the_cap(list_html: str, detail_html: str) -> None:
    for html in (list_html, detail_html):
        cleaned = clean_html(html)

        assert cleaned.chars <= DEFAULT_MAX_CHARS
        assert cleaned.chars < cleaned.original_chars


def test_narrows_before_truncating(list_html: str) -> None:
    """상한이 빡빡하면 먼저 반복 영역으로 좁힌다. 좁힌 사실은 결과에 남는다."""
    cleaned = clean_html(list_html, max_chars=4000)

    assert cleaned.narrowed is True
    assert cleaned.chars <= 4000
    assert "ol" in cleaned.html
    assert any("좁혔다" in note for note in cleaned.notes())


def test_truncation_is_reported() -> None:
    """좁혀도 상한을 못 맞추면 자르고, 잘랐다고 말한다."""
    html = "<html><body><main><p>" + ("가" * 5000) + "</p></main></body></html>"

    cleaned = clean_html(html, max_chars=1000)

    assert cleaned.truncated is True
    assert cleaned.chars == 1000
    assert any("잘랐다" in note for note in cleaned.notes())


def test_clean_page_reports_nothing_removed() -> None:
    cleaned = clean_html("<html><body><h1>공고 하나</h1></body></html>")

    assert cleaned.notes() == []
    assert cleaned.narrowed is False
    assert cleaned.truncated is False


def test_keep_siblings_must_be_positive() -> None:
    with pytest.raises(ValueError):
        clean_html("<ul><li>1</li></ul>", keep_siblings=0)


def test_section_blocks_with_different_classes_are_not_sampled() -> None:
    """태그만 같고 클래스가 다른 형제는 반복이 아니다. 다섯 번째 섹션의 공고 목록이 남아야 한다.

    recruiter.co.kr 실측(2026-09-22): 페이지 빌더가 섹션을 div 일곱 개로 두고 공고 목록이 다섯
    번째였다. 태그 이름만 보고 줄이면 목록이 통째로 지워져 셀렉터를 만들 수 없었다.
    """
    items = "".join(f'<li><a class="item" href="/jobs/{n}">공고 {n}</a></li>' for n in range(8))
    html = (
        "<body><div>"
        '<div class="banner">배너</div><div></div><div class="intro">소개</div><div></div>'
        f'<div class="list"><ul>{items}</ul></div><div></div><div></div>'
        "</div></body>"
    )

    cleaned = clean_html(html)
    soup = BeautifulSoup(cleaned.html, "html.parser")

    assert soup.select_one("div.list") is not None
    assert len(soup.select("div.list li")) == 4


def test_huge_attribute_values_are_cut_short() -> None:
    """이노션 실측(2026-09-22): 본문 속 `data-buffer` 하나가 14만 자라 입력 상한을 혼자 다 썼다."""
    html = f'<div class="body"><span data-buffer="{"A" * 150_000}">본문</span></div>'

    cleaned = clean_html(html)

    assert len(cleaned.html) < 1_000
    assert "data-buffer" in cleaned.html
    assert "본문" in cleaned.html
    assert not cleaned.truncated


def test_같은_클래스의_섹션이라도_안이_제각각이면_줄이지_않는다() -> None:
    """greetinghr 실측(2026-09-22): 같은 클래스의 섹션 여덟 개 중 일곱 번째가 공고 목록이었다."""
    sizes = [2, 5, 14, 4, 1, 9, 30, 2]
    sections = "".join(
        f'<div class="section"><div class="inner">{"<p>글</p>" * size}</div></div>'
        for size in sizes[:6]
    )
    items = "".join(
        f'<li><a class="item" href="/ko/o/{n}"><span>공고 {n}</span><span>경력</span></a></li>'
        for n in range(6)
    )
    sections += f'<div class="section"><div class="inner"><ul>{items}</ul></div></div>'
    sections += '<div class="section"><div class="inner"><p>끝</p></div></div>'

    cleaned = clean_html(f"<body><main>{sections}</main></body>")
    soup = BeautifulSoup(cleaned.html, "html.parser")

    assert len(soup.select("div.section")) == 8
    assert len(soup.select("a.item")) == 4
