"""목록이 없는 페이지에서 공고 목록 페이지 링크를 고른다 (`app/selector/list_page.py`).

모양은 11/16 일괄 추가에서 목록을 못 찾은 사이트들의 실제 메뉴다 (2026-11-22).
"""

from __future__ import annotations

from app.selector.list_page import list_page_candidates


def _page(*anchors: tuple[str, str]) -> str:
    return "<nav>" + "".join(f'<a href="{href}">{text}</a>' for href, text in anchors) + "</nav>"


def test_그리팅_첫_화면에서_채용_공고_메뉴를_고른다() -> None:
    html = _page(
        ("/ko", ""),
        ("/ko/team", "로앤 피플"),
        ("/ko/benefit", "로앤 복지"),
        ("/ko/guide", "합류 여정"),
        ("/ko/apply", "채용 공고"),
        ("/ko/apply", "채용 바로가기"),
        ("/ko/home", "개인정보 처리방침"),
    )

    found = list_page_candidates(html, "https://lawcompany.career.greetinghr.com/ko/home")

    assert found[0].url == "https://lawcompany.career.greetinghr.com/ko/apply"


def test_경로가_무작위인_나인하이어도_글자로_고른다() -> None:
    html = _page(("/", "회사 소개"), ("/faq", "FAQ"), ("/sou8f9wp", "채용 공고 바로가기 ↗︎"))

    found = list_page_candidates(html, "https://modetour.ninehire.site/")

    assert [item.url for item in found] == ["https://modetour.ninehire.site/sou8f9wp"]


def test_영문_메뉴도_고른다() -> None:
    html = _page(("/whoweare", "WHO WE ARE"), ("/openpositions", "OPEN POSITIONS"))

    found = list_page_candidates(html, "https://team.wiselycompany.com/")

    assert found[0].url == "https://team.wiselycompany.com/openpositions"


def test_공고_한_건_다른_호스트_지금_페이지_안내_페이지는_고르지_않는다() -> None:
    html = _page(
        ("/ko/home", "채용공고 바로가기"),
        ("/ko/o/113634", "인재풀 등록하기"),
        ("https://www.greetinghr.com/careers", "채용 공고"),
        ("/faq", "채용 FAQ"),
        ("/sign-in", "합격자 발표"),
    )

    assert list_page_candidates(html, "https://carat.career.greetinghr.com/ko/home") == []


def test_후보는_둘까지_점수_순이다() -> None:
    html = _page(
        ("/ko/guide1", "채용안내"),
        ("/ko/guide", "지원하기"),
        ("/ko/recruit", "Open Positions"),
    )

    found = list_page_candidates(html, "https://innospace.career.greetinghr.com/ko/careers")

    assert len(found) == 2
    assert found[0].url == "https://innospace.career.greetinghr.com/ko/recruit"
