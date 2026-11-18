"""같은 목록 페이지를 두 번 등록하지 않게 하는 주소 비교 (2026-11-18, 이삼오구)."""

from __future__ import annotations

import pytest

from app.api import ui_site_bulk
from app.api.crawlers import list_page_key


@pytest.mark.parametrize(
    "other",
    [
        "https://2359.co.kr/job_posting",
        "http://www.2359.co.kr/job_posting/",
        "https://WWW.2359.co.kr/job_posting#top",
    ],
)
def test_스킴_www_끝의_슬래시만_다르면_같은_목록이다(other: str) -> None:
    assert list_page_key(other) == list_page_key("https://www.2359.co.kr/job_posting")


@pytest.mark.parametrize(
    "other",
    [
        "https://www.2359.co.kr/job_posting?page=2",
        "https://www.2359.co.kr/careers",
        "https://jobs.2359.co.kr/job_posting",
    ],
)
def test_경로_쿼리_다른_서브도메인은_다른_목록이다(other: str) -> None:
    assert list_page_key(other) != list_page_key("https://www.2359.co.kr/job_posting")


def test_일괄_추가는_www_만_다른_주소를_중복으로_건너뛴다() -> None:
    rows = [
        ui_site_bulk.Row(2, "이삼오구", "https://2359.co.kr/job_posting", "", has_affiliates=False),
    ]
    taken = {ui_site_bulk._same_url("https://www.2359.co.kr/job_posting/")}

    ready, skipped = ui_site_bulk.sort_rows(rows, taken)

    assert ready == []
    assert [item.kind for item in skipped] == [ui_site_bulk.DUPLICATE]
