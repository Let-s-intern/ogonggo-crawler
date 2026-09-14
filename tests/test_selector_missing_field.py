"""모델이 필수 필드를 채우지 못했을 때 크롤러를 버리지 않는다.

통째로 거절하면 운영자가 손으로 고칠 대상조차 없다. `.claude/rules/llm.md` 는 손 편집을
첫 번째 수단으로 두므로, 빈 채로 draft 에 남기고 어느 자리가 비었는지 알려야 한다.
"""

from __future__ import annotations

import pytest

from app.crawler.parser import parse_list
from app.selector.schema import (
    ListSelectors,
    SelectorSchemaError,
    parse_selectors,
    parse_selectors_allowing_empty,
)

FULL = """
{"list": {"item": "li.job", "title": "h4", "link": "a", "date": "time"},
 "detail": {"title": "h1", "body": ".body", "qualifications": "", "recruitment_end_at": "",
 "department": ""}}
"""

MISSING_TITLE = """
{"list": {"item": "li.job", "title": "", "link": "a", "date": "time"},
 "detail": {"title": "h1", "body": ".body", "qualifications": "", "recruitment_end_at": "",
 "department": ""}}
"""

# 목록에 날짜를 안 적는 사이트가 있다. 빈 값이 거절 사유가 아니다 (`app/selector/schema.py`)
MISSING_DATE = """
{"list": {"item": "li.job", "title": "h4", "link": "a", "date": ""},
 "detail": {"title": "h1", "body": ".body", "qualifications": "", "recruitment_end_at": "",
 "department": ""}}
"""


def test_strict_parse_still_rejects_a_missing_field() -> None:
    """기본 경로는 그대로 엄격하다. 느슨한 판정이 새어 나가면 안 된다."""
    with pytest.raises(SelectorSchemaError) as caught:
        parse_selectors(MISSING_TITLE)

    assert caught.value.reason == "missing_field"


def test_빈_날짜는_거절하지_않는다() -> None:
    """목록에 날짜가 없는 사이트가 있다. 그 빈 값 하나로 크롤러를 못 돌리면 안 된다."""
    selectors = parse_selectors(MISSING_DATE)

    assert selectors.list.date == ""
    assert selectors.list.title == "h4"


def test_lenient_parse_keeps_the_selector_and_names_the_empty_field() -> None:
    selectors, empty = parse_selectors_allowing_empty(MISSING_TITLE)

    assert empty == ["list.title"]
    # 비어 있는 자리는 비워 둔다. 무엇이었을지 추측해 채우지 않는다.
    assert selectors.list.title == ""
    # 나머지는 살아 있어야 손으로 고칠 값이 남는다.
    assert selectors.list.item == "li.job"
    assert selectors.list.date == "time"
    assert selectors.detail.title == "h1"


def test_lenient_parse_reports_nothing_when_the_response_is_complete() -> None:
    selectors, empty = parse_selectors_allowing_empty(FULL)

    assert empty == []
    assert selectors.list.date == "time"


def test_lenient_parse_still_rejects_an_unknown_field() -> None:
    """비어 있는 것은 봐주지만 지어낸 필드는 봐주지 않는다."""
    with pytest.raises(SelectorSchemaError) as caught:
        parse_selectors_allowing_empty(
            '{"list": {"item": "li", "title": "h4", "link": "a", "date": "t", "links": "x"},'
            ' "detail": {"title": "h1", "body": "b", "qualifications": "",'
            ' "recruitment_end_at": "", "department": ""}}'
        )

    assert caught.value.reason == "unknown_field"


def test_빈_셀렉터는_문법_오류가_아니라_빈_값이다() -> None:
    """모델이 채우지 못한 필드 하나가 목록 전체를 못 읽게 만들면 안 된다.

    2026-08-25 네이버 등록에서 `list.date` 가 빈 채로 저장됐고, 그것을 `select("")` 로
    돌린 파서가 문법 오류를 냈다. 항목 10건이 0건으로 읽혀 등록이 통째로 실패했다.
    """
    html = """
    <html><body><ul>
      <li class="card"><h4>첫 공고</h4><a href="/jobs/1">보기</a></li>
      <li class="card"><h4>둘째 공고</h4><a href="/jobs/2">보기</a></li>
    </ul></body></html>
    """
    selectors = ListSelectors(item="li.card", title="h4", link="a", date="")

    result = parse_list(html, selectors, "https://example.test/jobs")

    assert [item.title for item in result.items] == ["첫 공고", "둘째 공고"]
    assert [item.date for item in result.items] == ["", ""]
