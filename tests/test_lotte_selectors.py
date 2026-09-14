"""롯데 셀렉터를 2026-08-25 픽스처에 돌려 본다. 실사이트에 나가지 않는다.

`detail.qualifications` 가 비어 있어 8건 전부 자격요건이 빈 값으로 들어왔다
(`.claude/tasks/done/fill-body/tasks-fill-body-push4.md` 4.5). 자격요건은 본문 안
`응시자격` 제목 다음 목록에 있다.

제목으로 찾는다. 상세마다 절이 몇 개인지가 다르고, `n번째 ul` 로 잡으면 절 하나가 늘거나 줄 때
엉뚱한 값이 자격요건으로 들어온다. `:-soup-contains()` 는 soupsieve 의 확장이고 파서가 이미
soupsieve 로 셀렉터를 돌린다.

여기 적힌 셀렉터는 `crawlers.selectors_json` 에 저장된 롯데 값과 같아야 한다.
"""

from __future__ import annotations

import pathlib

from app.crawler.parser import parse_detail, parse_list
from app.selector.schema import validate_selectors

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
LIST_URL = "https://recruit.lotte.co.kr/apply/announcement"

REQUIREMENTS = '.board-content p.hire-title:-soup-contains("자격") + ul.hire-bul'

SELECTORS = validate_selectors(
    {
        "list": {
            "item": "ul.job-card-list > li",
            "title": ".card-tit a",
            "link": ".card-tit a",
            "date": ".card-foot .date",
            "company_name": ".cmp-name",
            "link_template": "",
        },
        "detail": {
            "title": "h4.title",
            "body": ".board-content",
            "qualifications": REQUIREMENTS,
            "recruitment_end_at": ".date-detail",
            "department": "",
            "company_name": ".board-type",
        },
    }
)


def test_the_qualifications_come_out_of_the_detail_page() -> None:
    """빈 값이던 자리에 응시자격이 들어온다."""
    html = (FIXTURES / "lotte-detail-20260825.html").read_text(encoding="utf-8")

    result = parse_detail(html, SELECTORS.detail)

    qualifications = result.fields["qualifications"]
    assert qualifications.strip()
    assert "4년제 학사" in qualifications
    assert "국가보훈대상자" in qualifications


def test_the_qualifications_are_not_the_whole_body() -> None:
    """본문 전체를 자격요건으로 밀어 넣은 것이 아니다."""
    html = (FIXTURES / "lotte-detail-20260825.html").read_text(encoding="utf-8")

    result = parse_detail(html, SELECTORS.detail)

    assert "전형절차" not in result.fields["qualifications"]
    assert len(result.fields["qualifications"]) < len(result.fields["body"])


def test_the_html_in_the_body_is_left_for_normalization() -> None:
    """줄바꿈과 공백은 여기서 정리하지 않는다. 정규화 규칙의 몫이다."""
    html = (FIXTURES / "lotte-detail-20260825.html").read_text(encoding="utf-8")

    result = parse_detail(html, SELECTORS.detail)

    assert "\n" in result.fields["body"]
    assert result.fields["recruitment_end_at"] == "2026.08.20 ~ 2026.08.31"


def test_the_list_still_yields_eight_postings() -> None:
    """자격요건을 고치면서 목록 셀렉터가 흔들리지 않았는지 함께 본다."""
    html = (FIXTURES / "lotte-list-20260825.html").read_text(encoding="utf-8")

    result = parse_list(html, SELECTORS.list, LIST_URL)

    assert result.matched == 8
    assert len(result.items) == 8
    assert all(item.link.startswith(f"{LIST_URL}/detail/") for item in result.items)
    assert len({item.link for item in result.items}) == 8
