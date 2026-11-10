"""목록 0건인데 페이지가 공고가 없다고 말하는 사이트 (2026-11-10 결정).

| 확인 | 깨지면 |
|---|---|
| 한국어·영어 빈 목록 문구를 알아본다 | 공고 없는 회사가 매번 `고치기` 로 뜬다 |
| `<script>`·`<template>` 안 문구는 안 본다 | 자바스크립트로 그리는 사이트가 늘 공고 없음이 된다 |
| 문구가 없으면 지금처럼 `selector_miss` 다 | 셀렉터가 깨진 사이트가 공고 없음에 묻힌다 |
API 쪽은 `tests/test_api_source.py` 에 있다.
"""

from __future__ import annotations

import pytest

from app.crawler.empty_list import empty_notice
from app.crawler.parser import EmptyListError, SelectorMissError, parse_list
from app.selector.schema import validate_selectors

SELECTORS = validate_selectors(
    {
        "list": {"item": "ul.jobs > li", "title": "a", "link": "a", "date": ""},
        "detail": {
            "title": "h1",
            "body": "main",
            "qualifications": "",
            "recruitment_end_at": "",
            "department": "",
        },
    }
).list


@pytest.mark.parametrize(
    "text",
    [
        "현재 진행중인 채용공고가 없습니다.",
        "등록된 공고가 없습니다",
        "진행 중인 포지션이 없어요",
        "검색 결과가 없습니다",
        "등록된 게시물이 없습니다.",
        "There are currently no open positions.",
        "No open positions at the moment",
        "No jobs found",
    ],
)
def test_빈_목록_문구를_알아본다(text: str) -> None:
    assert empty_notice(f"<html><body><main><p>{text}</p></main></body></html>")


def test_공고가_있는_페이지는_빈_목록이_아니다() -> None:
    html = "<ul class='jobs'><li><a href='/1'>백엔드 개발자</a></li></ul><p>채용 문의는 메일로</p>"
    assert empty_notice(html) == ""


def test_보이지_않는_문구는_보지_않는다() -> None:
    html = (
        "<body><div id='app'></div>"
        "<template><p>공고가 없습니다</p></template>"
        "<script>const empty = '진행중인 채용이 없습니다'</script></body>"
    )
    assert empty_notice(html) == ""


def test_문구는_앞뒤_글자와_함께_돌려준다() -> None:
    notice = empty_notice("<p>안내: 현재 진행중인 채용공고가 없습니다. 다음에 다시 와 주세요</p>")
    assert "채용공고가 없" in notice
    assert len(notice) < 80


def test_0개_매칭에_빈_목록_문구가_있으면_EmptyListError_다() -> None:
    html = "<ul class='jobs'></ul><p class='empty'>진행 중인 채용이 없습니다</p>"

    with pytest.raises(EmptyListError) as caught:
        parse_list(html, SELECTORS, "https://example.com/jobs")

    assert "채용이 없" in caught.value.notice
    # 사이트를 찾는 경로는 여전히 셀렉터 미스로 받는다
    assert isinstance(caught.value, SelectorMissError)


def test_0개_매칭에_문구가_없으면_selector_miss_다() -> None:
    with pytest.raises(SelectorMissError) as caught:
        parse_list("<div class='list'></div>", SELECTORS, "https://example.com/jobs")

    assert not isinstance(caught.value, EmptyListError)
