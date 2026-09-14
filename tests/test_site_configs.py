"""열한 사이트 설정을 저장된 픽스처에 그대로 돌려 본다. 실사이트에 나가지 않는다.

`seeds/site-configs-20260826.json` 이 `crawlers` 행에 들어가는 값이다. 실사이트 요청은 등록
직후 한 번뿐이라, 그 전에 설정이 실제 응답에서 무엇을 뽑는지를 여기서 다 본다.

각 사이트에서 확인하는 것은 같다 — 목록에서 측정한 건수가 나오는가, 공고마다 다른 주소가
만들어지는가, 상세에서 **본문이 비어 있지 않은가**.

칸별로 무엇이 채워지고 무엇이 비는지는 `tests/test_split_body_mapping.py` 가 본다. 여기는
목록과 본문까지다.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from app.crawler.api_source import build_detail, build_html_items, build_items
from app.crawler.parser import parse_detail, parse_list
from app.selector.api_schema import ApiConfig, validate_api_config
from app.selector.schema import SelectorSet, validate_selectors

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SEEDS = pathlib.Path(__file__).parent.parent / "seeds" / "site-configs-20260826.json"

CONFIGS: dict[str, dict[str, Any]] = {
    entry["name"]: entry for entry in json.loads(SEEDS.read_text(encoding="utf-8"))["crawlers"]
}


def api_config(name: str) -> ApiConfig:
    return validate_api_config(CONFIGS[name]["api_config"])


def selectors(name: str) -> SelectorSet:
    return validate_selectors(CONFIGS[name]["selectors"])


def payload(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_every_crawler_config_is_valid() -> None:
    """저장하기 전에 열한 개가 다 스키마를 지나는지 본다."""
    assert len(CONFIGS) == 11
    for name, entry in CONFIGS.items():
        assert entry["list_mode"] in ("static", "api", "playwright"), name
        assert entry["detail_mode"] in ("static", "api", "playwright"), name
        if "api_config" in entry:
            validate_api_config(entry["api_config"])
        if "selectors" in entry:
            validate_selectors(entry["selectors"])


def test_no_crawler_dresses_itself_up_as_a_browser() -> None:
    """헤더에 담은 것은 사이트가 요구하는 기능성 헤더뿐이다."""
    for name, entry in CONFIGS.items():
        for section in entry.get("api_config", {}).values():
            names = {key.lower() for key in section.get("headers", {})}
            assert "user-agent" not in names, name
            assert "cookie" not in names, name


def test_every_site_that_reaches_detail_says_where_the_body_is() -> None:
    """본문 없이 등록된 크롤러가 없어야 한다. 이 Push 의 목적이 그것이다."""
    for name, entry in CONFIGS.items():
        detail = entry.get("api_config", {}).get("detail")
        if detail is not None:
            assert detail["fields"].get("body"), name
            continue
        assert entry["selectors"]["detail"]["body"], name


# 사이트별 확인 --------------------


def test_lg_reads_eighty_eight_postings_and_a_body_per_sector() -> None:
    config = api_config("LG")

    listing = build_items(payload("lg-list-20260825.json"), config.list_config())
    detail = build_detail(payload("lg-detail-20260825.json"), config.detail_config())

    assert listing.matched == 88
    assert len(listing.items) == 88
    assert len({item.link for item in listing.items}) == 88
    assert listing.items[0].date == "2026.09.13 23:00"
    assert detail.fields["body"].strip()
    assert detail.fields["qualifications"] == ""
    assert detail.fields["recruitment_end_at"] == "2026.09.13 23:00"


def test_hanwha_reads_a_page_of_twenty_and_a_job_body() -> None:
    config = api_config("한화")

    listing = build_items(payload("hanwha-list-p0-20260825.json"), config.list_config())
    detail = build_detail(payload("hanwha-detail-20260825.json"), config.detail_config())

    assert len(listing.items) == 20
    assert listing.items[0].company_name == "한화생명"
    assert listing.items[0].link.endswith("detail?rtSeq=19463")
    assert "LIFEPLUS TV" in detail.fields["body"]
    assert detail.fields["qualifications"] == ""
    assert detail.fields["recruitment_end_at"] == "2026.08.25 15:00"


def test_samsung_reads_nine_on_the_first_page_and_a_body_per_role() -> None:
    config = api_config("삼성")

    listing = build_html_items(html("samsung-list-p1-20260825.html"), config.list_config())
    detail = build_detail(payload("samsung-detail-20260825.json"), config.detail_config())

    assert len(listing.items) == 9
    assert listing.items[0].detail_key == "22878"
    assert "seqno=22878" in listing.items[0].link
    assert "~" in listing.items[0].date
    assert detail.fields["body"].strip()
    assert detail.fields["qualifications"] == ""
    # 마감일은 목록의 기간에서 온다. 상세에는 적지 않았다
    assert not detail.fields["recruitment_end_at"]


def test_sk_reads_a_hundred_and_four_and_a_server_rendered_detail() -> None:
    config = api_config("SK")
    selector_set = selectors("SK")

    listing = build_items(payload("sk-list-20260825.json"), config.list_config())
    detail = parse_detail(html("sk-detail-20260825.html"), selector_set.detail)

    assert listing.matched == 104
    assert len(listing.items) == 104
    assert len({item.link for item in listing.items}) == 104
    assert listing.items[0].link == "https://www.skcareers.com/Recruit/Detail/R261752"
    assert detail.fields["body"].strip()
    assert detail.fields["qualifications"] == ""
    assert "August 25, 2026" in detail.fields["recruitment_end_at"]


def test_hyundai_reads_twenty_and_a_plain_text_body() -> None:
    config = api_config("현대자동차")

    listing = build_items(payload("hyundai-list-20260825.json"), config.list_config())
    detail = build_detail(payload("hyundai-detail-02800-20260825.json"), config.detail_config())

    assert len(listing.items) == 20
    assert len({item.link for item in listing.items}) == 20
    assert listing.items[0].link.endswith("recuYy=2026&recuType=N2&recuCls=295")
    assert detail.fields["body"].strip()
    assert detail.fields["qualifications"] == ""
    # 조직·부서도 이제 수집하지 않는다. 본문을 나누는 쪽이 채운다 (1.2)
    assert detail.fields["department"] == ""


def test_lotte_reads_eight_and_carries_the_qualifications_inside_the_body() -> None:
    selector_set = selectors("롯데그룹")

    listing = parse_list(
        html("lotte-list-20260825.html"),
        selector_set.list,
        CONFIGS["롯데그룹"]["list_url"],
    )
    detail = parse_detail(html("lotte-detail-20260825.html"), selector_set.detail)

    assert len(listing.items) == 8
    assert detail.fields["body"].strip()
    # 자격요건은 이제 수집이 아니라 본문을 나누는 쪽이 채운다 (1.2)
    assert "4년제 학사" in detail.fields["body"]


# 마감 건너뜀 --------------------


@pytest.mark.parametrize("name", ["LG", "한화", "삼성", "SK", "현대자동차"])
def test_the_sites_whose_list_date_is_a_deadline_say_so(name: str) -> None:
    """다섯 사이트 모두 목록 응답에 마감일이 들어 있다. 그 사실을 설정에 적었다."""
    assert api_config(name).list_config().date_is_deadline is True


def test_lotte_leaves_the_judgement_to_its_detail_selector() -> None:
    """롯데는 목록이 정적 HTML 이라 API 설정 자체가 없다. 상세가 마감일을 준다."""
    assert "api_config" not in CONFIGS["롯데그룹"]
    assert selectors("롯데그룹").detail.recruitment_end_at
