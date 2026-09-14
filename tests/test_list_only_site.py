"""상세 페이지가 없는 사이트도 등록되고 실행된다.

삼성처럼 상세를 JS 로 그려 별도 주소가 없는 사이트가 있다. `list.link` 를 필수로 두면
그런 사이트는 등록 자체가 안 되고 워크플로우까지 갈 수 없다.

셀렉터가 비어 있는 것과, 셀렉터가 있는데 0개 매칭인 것은 다르다. 앞은 없는 것이고 뒤는
실패다 — 화면의 `건너뜀` / `실패` 구분과 같은 기준이다.
"""

from __future__ import annotations

import pytest

from app.crawler.parser import FieldParseError, list_only, parse_list
from app.selector.schema import ListSelectors

LIST_HTML = """
<ul class="jobs">
  <li><h3>백엔드 개발자</h3><span class="d">2026-09-30</span></li>
  <li><h3>프론트엔드 개발자</h3><span class="d">2026-10-15</span></li>
</ul>
"""

WITH_LINK_HTML = """
<ul class="jobs">
  <li><h3><a href="/jobs/1">백엔드 개발자</a></h3><span class="d">2026-09-30</span></li>
</ul>
"""

NO_LINK = ListSelectors(
    item="ul.jobs li", title="h3", link="", date=".d", company_name="", link_template=""
)
BROKEN_LINK = ListSelectors(
    item="ul.jobs li", title="h3", link="a.detail", date=".d", company_name="", link_template=""
)


def test_list_only_is_true_when_both_link_fields_are_empty() -> None:
    assert list_only(NO_LINK) is True
    assert list_only(BROKEN_LINK) is False


def test_a_site_without_detail_links_still_yields_items() -> None:
    result = parse_list(LIST_HTML, NO_LINK, "https://example.test/jobs")

    assert [item.title for item in result.items] == ["백엔드 개발자", "프론트엔드 개발자"]
    # 상세로 갈 길이 없으므로 목록 주소가 남는다
    assert {item.link for item in result.items} == {"https://example.test/jobs"}
    assert all(item.detail_absent for item in result.items)
    assert result.failures == []


def test_a_selector_that_matches_nothing_is_still_a_failure() -> None:
    """셀렉터를 적어 뒀는데 못 찾은 것은 여전히 실패다. 이것까지 넘기면 안 된다."""
    with pytest.raises(FieldParseError) as caught:
        parse_list(LIST_HTML, BROKEN_LINK, "https://example.test/jobs")

    assert "link" in str(caught.value)


def test_a_normal_site_is_unaffected() -> None:
    selectors = ListSelectors(
        item="ul.jobs li", title="h3", link="a", date=".d", company_name="", link_template=""
    )

    result = parse_list(WITH_LINK_HTML, selectors, "https://example.test/jobs")

    assert result.items[0].link == "https://example.test/jobs/1"
    assert result.items[0].detail_absent is False


def test_a_list_only_record_takes_title_and_deadline_from_the_list() -> None:
    """상세를 안 따라가면 title 이 올 곳은 목록뿐이다. 비워 두면 공고를 알아볼 수 없다."""
    from app.crawler.parser import ListItem
    from app.crawler.runner import _record
    from app.selector.schema import DETAIL_FIELDS

    item = ListItem(
        index=0,
        title="백엔드 개발자",
        link="https://example.test/jobs",
        date="2026-09-30",
        company_name="삼성",
        detail_absent=True,
    )

    record = _record(item, dict.fromkeys(DETAIL_FIELDS, ""))

    assert record["title"] == "백엔드 개발자"
    assert record["recruitment_end_at"] == "2026-09-30"
    assert record["company_name"] == "삼성"
    # 상세에서 오는 값은 비어 있는 것이 맞다. 없는 페이지를 지어내지 않는다
    assert record["body"] == ""


def test_a_normal_record_still_prefers_the_detail_page() -> None:
    from app.crawler.parser import ListItem
    from app.crawler.runner import _record

    item = ListItem(index=0, title="목록 제목", link="https://x/1", date="2026-01-01")
    detail = {
        "title": "상세 제목",
        "body": "본문",
        "qualifications": "",
        "recruitment_end_at": "2026-12-31",
        "department": "",
        "company_name": "",
    }

    record = _record(item, detail)

    assert record["title"] == "상세 제목"
    assert record["recruitment_end_at"] == "2026-12-31"


# 목록 전용 크롤러의 상세 필드는 실패가 아니다 (20.2 보정) --------------------


def test_the_test_screen_does_not_call_a_list_only_detail_field_a_failure() -> None:
    """LG 실행에서 `detail.body` 가 `실패` 로 떴다. 고칠 수 없는 것을 고치라는 표시다.

    상세로 갈 길이 없으면 상세 페이지를 아예 열지 않는다. `detail.title` 과
    `detail.recruitment_end_at` 에 값이 있는 것은 실행이 목록에서 읽은 값을 그 자리에 넣기 때문이고
    (`app/crawler/runner.py` 의 `_record`), `body` 는 목록에 없어서 빌 뿐이다.
    """
    from app.api.ui_tests import _field_report
    from app.selector.schema import validate_selectors

    selectors = validate_selectors(
        {
            "list": {
                "item": "ul.jobs > li",
                "title": "h3",
                "link": "",
                "date": "span.d",
                "link_template": "",
            },
            "detail": {
                "title": "p.title",
                "body": "div.body",
                "qualifications": "",
                "recruitment_end_at": "div.deadline",
                "department": "",
            },
        }
    )
    items = [
        _PreviewItem(
            {
                "list_title": "백엔드 개발자",
                "list_date": "2026-09-30",
                "title": "백엔드 개발자",
                "recruitment_end_at": "2026-09-30",
                "body": "",
            }
        ),
        _PreviewItem(
            {
                "list_title": "프론트엔드 개발자",
                "list_date": "2026-10-15",
                "title": "프론트엔드 개발자",
                "recruitment_end_at": "2026-10-15",
                "body": "",
            }
        ),
    ]

    report = {row["path"]: row for row in _field_report(items, selectors)}

    assert report["detail.body"]["state"] == "건너뜀"
    assert "상세 페이지를 따라가지 않는" in report["detail.body"]["reason"]
    # 값이 있는 자리도 상세에서 온 것이 아니다. 어디서 왔는지 사유에 적는다
    assert report["detail.title"]["state"] == "건너뜀"
    assert "목록에서 읽은 것이다" in report["detail.title"]["reason"]
    # 목록 필드의 판정은 그대로다
    assert report["list.title"]["state"] == "성공"


def test_a_site_with_detail_links_still_fails_a_broken_detail_field() -> None:
    """상세를 따라가는 크롤러에서 상세 필드가 비면 그것은 여전히 실패다."""
    from app.api.ui_tests import _field_report
    from app.selector.schema import validate_selectors

    selectors = validate_selectors(
        {
            "list": {
                "item": "ul.jobs > li",
                "title": "h3",
                "link": "h3 > a",
                "date": "span.d",
                "link_template": "",
            },
            "detail": {
                "title": "p.title",
                "body": "div.body",
                "qualifications": "",
                "recruitment_end_at": "",
                "department": "",
            },
        }
    )
    items = [
        _PreviewItem(
            {
                "list_title": "백엔드 개발자",
                "list_date": "2026-09-30",
                "title": "백엔드 개발자",
                "body": "",
            }
        )
    ]

    report = {row["path"]: row for row in _field_report(items, selectors)}

    assert report["detail.body"]["state"] == "실패"
    assert "selector_miss" in report["detail.body"]["reason"]


class _PreviewItem:
    """`_field_report` 가 읽는 것만 흉내낸다."""

    def __init__(self, fields: dict[str, str]) -> None:
        self.fields = fields
        self.state = "preview"
