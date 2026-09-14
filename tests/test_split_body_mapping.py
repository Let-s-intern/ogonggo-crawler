"""열한 사이트가 여섯 칸만 수집하는지 픽스처로 확인한다 (1.2.V).

**수집은 여섯 칸만 한다** — 제목·본문·모집 시작일·모집 마감일·회사명·원본 주소. 나머지 열한
칸은 본문을 읽어 나눈다 (`app/classify/`).

이 파일은 하루 사이에 뜻이 뒤집혔다. 아침에는 "설정에 적힌 자리가 그 칸을 채우는가" 를
물었고, 지금은 **"여섯 칸 말고는 수집이 채우지 않는가"** 를 묻는다. 매핑 방식이 640건에서
절반도 못 채웠고 176번의 판단 중 다섯 곳이 뜻이 다른 칸에 들어가 있었기 때문이다
(`.claude/tasks/memos/보류/llm-classify/prd-llm-classify.md`, `seeds/site-configs-20260826.json` 의
`why_the_mappings_were_removed`).

빈 칸도 단언한다. 매핑이 하나라도 되살아나면 그 값이 분류 결과를 이기고 (정규화는 규칙이
만든 값을 분류보다 먼저 둔다) 조용히 옛 방식으로 돌아간다.

실사이트에 나가지 않는다. 열한 사이트의 응답이 전부 `tests/fixtures/` 에 있다.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest

from app.crawler.api_source import build_detail, build_items
from app.crawler.parser import parse_detail
from app.selector.api_schema import validate_api_config
from app.selector.schema import DETAIL_FIELDS, validate_selectors

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SEEDS = pathlib.Path(__file__).parent.parent / "seeds" / "site-configs-20260826.json"

SEED_DOC: dict[str, Any] = json.loads(SEEDS.read_text(encoding="utf-8"))
CONFIGS: dict[str, dict[str, Any]] = {entry["name"]: entry for entry in SEED_DOC["crawlers"]}

JSON = "json"
HTML = "html"

# 사이트마다 상세를 어디서 읽는지. `json` 이면 API 설정, `html` 이면 셀렉터다
DETAIL_SOURCE: dict[str, tuple[str, str]] = {
    "LG": (JSON, "lg-detail-20260825.json"),
    "한화": (JSON, "hanwha-detail-20260825.json"),
    "삼성": (JSON, "samsung-detail-20260825.json"),
    "현대자동차": (JSON, "hyundai-detail-02800-20260825.json"),
    "SK": (HTML, "sk-detail-20260825.html"),
    "롯데그룹": (HTML, "lotte-detail-20260825.html"),
    "두산": (HTML, "doosan-detail-1000361539-20260826.html"),
    "네이버": (HTML, "naver-detail-30005299-20260826.html"),
    "토스": (HTML, "toss-detail-7827417003-20260826.html"),
    "카카오": (HTML, "kakao-detail-P-14503-20260826.html"),
    "우아한형제들": (HTML, "woowa-detail-R2607031-20260826.html"),
}

# 상세가 못 주는 칸을 목록이 나르는 사이트. 그 목록 픽스처다
# (`app/crawler/runner.py` 의 `_record`)
LIST_SOURCE: dict[str, str] = {
    "카카오": "kakao-list-api-20260825.json",
    "우아한형제들": "woowa-list-api-20260825.json",
}

# 수집이 채우는 칸. 여섯 중 `title`·`body`·`source_url` 은 어디나 채우므로 적지 않는다 —
# 앞의 둘이 비면 수집 자체가 실패다 (`app/crawler/parser.py` 의 `REQUIRED_DETAIL_FIELDS`).
#
# 여기 없는 칸은 비어야 한다. 사이트가 그 값을 별도 필드로 주더라도 수집하지 않는다 —
# 176번의 매핑 판단을 없애는 것이 이 변경이고, 한 사이트만 예외를 두면 그 판단이 돌아온다.
FILLED: dict[str, frozenset[str]] = {
    "LG": frozenset({"recruitment_end_at", "company_name", "recruitment_start_at"}),
    "한화": frozenset({"recruitment_end_at", "company_name", "recruitment_start_at"}),
    # 삼성 상세 응답에는 마감일 자리가 없다. 목록의 날짜가 마감일로 들어온다
    "삼성": frozenset({"company_name", "recruitment_start_at"}),
    # 현대는 목록에도 상세에도 회사명이 없다. 크롤러 이름이 정규화에서 채운다 (1.3)
    "현대자동차": frozenset({"recruitment_end_at", "recruitment_start_at"}),
    "SK": frozenset({"recruitment_end_at", "company_name", "recruitment_start_at"}),
    "롯데그룹": frozenset({"recruitment_end_at", "company_name", "recruitment_start_at"}),
    "두산": frozenset({"recruitment_end_at", "company_name", "recruitment_start_at"}),
    "네이버": frozenset({"recruitment_end_at", "company_name", "recruitment_start_at"}),
    # 토스는 목록이 회사명을 주지 않고 대부분의 공고에 모집 기간도 적지 않는다
    "토스": frozenset({"recruitment_end_at"}),
    "카카오": frozenset({"recruitment_end_at", "company_name", "recruitment_start_at"}),
    # 우아한형제들도 목록이 회사명을 주지 않는다
    "우아한형제들": frozenset({"recruitment_end_at", "recruitment_start_at"}),
}

# 수집이 더 이상 채우지 않는 칸. 본문을 읽어 나눈 결과가 채운다
CLASSIFIED: tuple[str, ...] = (
    "job_category",
    "employment_type",
    "experience_type",
    "region",
    "headcount",
    "responsibilities",
    "preferred_qualifications",
    "hiring_process",
    "qualifications",
    "department",
    "recruitment_notice",
)

SITES = tuple(DETAIL_SOURCE)


def _payload(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def collected(site: str) -> dict[str, str]:
    """그 사이트의 설정으로 픽스처에서 뽑은 칸별 값. 실행이 `raw_jobs` 에 싣는 것과 같다."""
    entry = CONFIGS[site]
    kind, fixture = DETAIL_SOURCE[site]
    if kind == JSON:
        config = validate_api_config(entry["api_config"]).detail_config()
        fields = dict(build_detail(_payload(fixture), config).fields)
    else:
        selectors = validate_selectors(entry["selectors"]).detail
        html = (FIXTURES / fixture).read_text(encoding="utf-8")
        fields = dict(parse_detail(html, selectors).fields)

    carried = _carried(site)
    return {name: fields.get(name, "") or carried.get(name, "") for name in DETAIL_FIELDS}


def _carried(site: str) -> dict[str, str]:
    """목록이 나르는 값. 그런 사이트가 아니면 빈 값이다."""
    if site not in LIST_SOURCE:
        return {}
    config = validate_api_config(CONFIGS[site]["api_config"]).list_config()
    listing = build_items(_payload(LIST_SOURCE[site]), config)
    return dict(listing.items[0].extra)


def test_every_site_in_the_table_has_a_config() -> None:
    """설정·픽스처·표가 같은 열한 곳을 가리키는지. 하나가 빠지면 조용히 안 돌아간다."""
    assert set(CONFIGS) == set(SITES)
    assert set(FILLED) == set(SITES)


@pytest.mark.parametrize("site", SITES)
def test_the_config_passes_its_own_schema(site: str) -> None:
    """저장하기 전에 열한 개가 다 스키마를 지나는지 본다."""
    entry = CONFIGS[site]
    if "api_config" in entry:
        validate_api_config(entry["api_config"])
    if "selectors" in entry:
        validate_selectors(entry["selectors"])


@pytest.mark.parametrize("site", SITES)
def test_the_columns_the_site_gives_are_filled(site: str) -> None:
    values = collected(site)

    empty = sorted(name for name in FILLED[site] if not values[name].strip())
    assert empty == [], f"{site} 가 준다고 적힌 칸이 비었다"


@pytest.mark.parametrize("site", SITES)
def test_the_columns_the_site_does_not_give_stay_empty(site: str) -> None:
    """빈 칸이 다른 값으로 채워지면 그 값이 분류 결과를 이긴다."""
    values = collected(site)
    expected_empty = set(DETAIL_FIELDS) - FILLED[site] - {"title", "body"}

    filled = sorted(name for name in expected_empty if values[name].strip())
    assert filled == [], f"{site} 가 주지 않는 칸에 값이 들어갔다"


@pytest.mark.parametrize("site", SITES)
def test_the_title_and_the_body_are_never_empty(site: str) -> None:
    values = collected(site)

    assert values["title"].strip(), site
    assert values["body"].strip(), site


def test_the_eleven_columns_are_not_collected_any_more() -> None:
    """열한 칸이 어느 사이트에서도 수집되지 않는지 한자리에서 본다."""
    for site in SITES:
        values = collected(site)
        filled = sorted(name for name in CLASSIFIED if values[name].strip())
        assert filled == [], f"{site} 가 분류에 맡긴 칸을 아직 수집하고 있다"


def test_toss_body_is_the_posting_and_not_the_navigation() -> None:
    """`.p-container__inner` 가 상단 내비게이션을 먼저 잡고 있었다. 본문은 분류의 재료다."""
    values = collected("토스")

    assert "토스인컴" in values["body"]
    assert not values["body"].startswith("계열사 소개")


def test_doosan_reads_its_deadline_from_the_label_not_the_row_number() -> None:
    """자리로 잡고 있어서 '지원자 개별일정' 이 마감일로 들어오고 있었다. 마감일은 남는 칸이다."""
    values = collected("두산")

    assert values["recruitment_end_at"].strip() == "2026-07-15 ~ 2026-08-31"
    assert values["company_name"].strip() == "매거진"


def test_naver_keeps_the_affiliate_in_the_company_column() -> None:
    """모집 부서가 'NAVER' 즉 계열사다. 회사명은 본문에서 못 뽑아 수집으로 남긴 칸이다."""
    values = collected("네이버")

    assert values["company_name"].strip() == "NAVER"
