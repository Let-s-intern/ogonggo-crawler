"""상세 원문(`source_text`)이 무엇을 담고 무엇을 담지 않는지 픽스처로 본다.

무엇을 컨테이너로 잡을지는 열한 픽스처를 재고 정했다
(`.claude/site-recipes/source-text-container.md`). 여기서는 그 결정이 실제 픽스처에서
그대로 나오는지를 사이트별로 확인한다. 네트워크에 나가지 않는다.

셀렉터는 `seeds/site-configs-20260826.json` 에서 읽는다. 문서에 사본을 두지 않는 것과 같은
이유로 테스트에도 사본을 두지 않는다 — 저장된 설정이 바뀌면 이 테스트가 같이 바뀌어야 한다.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any

import pytest
from bs4 import BeautifulSoup

from app.crawler.api_source import build_detail
from app.crawler.parser import parse_detail, source_text
from app.selector.api_schema import validate_api_config
from app.selector.schema import DetailSelectors, validate_selectors

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SEEDS = pathlib.Path(__file__).parent.parent / "seeds" / "site-configs-20260826.json"

CONFIGS: dict[str, dict[str, Any]] = {
    entry["name"]: entry for entry in json.loads(SEEDS.read_text(encoding="utf-8"))["crawlers"]
}

# 상세가 HTML 인 일곱 곳. 나머지 넷은 상세가 API 라 응답 전체를 편다 (`API_DETAIL`)
HTML_DETAIL: dict[str, str] = {
    "SK": "sk-detail-20260825.html",
    "롯데그룹": "lotte-detail-20260825.html",
    "두산": "doosan-detail-1000361539-20260826.html",
    "네이버": "naver-detail-30005299-20260826.html",
    "토스": "toss-detail-7827417003-20260826.html",
    "카카오": "kakao-detail-P-14503-20260826.html",
    "우아한형제들": "woowa-detail-R2607031-20260826.html",
}

# 태그가 남았는지 본다. 여는 태그와 닫는 태그 둘 다
TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*[\s/>]")


def html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def detail_selectors(name: str) -> DetailSelectors:
    return validate_selectors(CONFIGS[name]["selectors"]).detail


def parsed(name: str) -> Any:
    return parse_detail(html(HTML_DETAIL[name]), detail_selectors(name))


@pytest.mark.parametrize("site", sorted(HTML_DETAIL))
def test_every_html_detail_gives_a_source_text_that_holds_the_body(site: str) -> None:
    """일곱 곳 전부 원문이 나오고, 본문은 그 안에 통째로 들어 있다."""
    result = parsed(site)
    body = result.fields["body"]

    assert result.source_text.strip(), site
    assert body.strip() in result.source_text
    assert len(result.source_text) >= len(body)


@pytest.mark.parametrize("site", sorted(HTML_DETAIL))
def test_the_source_text_is_text_and_not_markup(site: str) -> None:
    """사람이 드래그해서 복사한 것과 같은 수준이다 — 태그는 없고 줄바꿈은 살아 있다."""
    text = parsed(site).source_text

    assert TAG.search(text) is None, text[:200]
    assert "\n" in text


@pytest.mark.parametrize(
    ("site", "expected"),
    [
        # 본문 밖에 이름표로 붙어 있던 값들. 이것을 담으려고 부모를 쓴다
        ("SK", ("SK biopharmaceuticals", "Gyeonggi/Incheon")),
        ("롯데그룹", ("2026.08.20 ~ 2026.08.31", "접수중")),
        ("네이버", ("모집 부서", "모집 경력")),
        ("카카오", ("영입마감일", "판교")),
        ("우아한형제들", ("신입/경력", "사업관리")),
    ],
)
def test_the_source_text_carries_the_labelled_values_the_body_left_out(
    site: str, expected: tuple[str, ...]
) -> None:
    """본문만 담으면 회사·기간·근무지·경력이 원문 밖에 남는다."""
    result = parsed(site)

    for value in expected:
        assert value not in result.fields["body"], value
        assert value in result.source_text, value


@pytest.mark.parametrize(
    ("site", "unwanted"),
    [
        # GNB·푸터. 부모 한 단계에서는 애초에 닿지 않는 자리다
        ("SK", ("Areas of Work", "Log In")),
        ("롯데그룹", ("사이트맵", "채용서류반환청구")),
        ("두산", ("두산스토리", "경영철학")),
        ("네이버", ("Search Jobs", "Benefits")),
        ("토스", ("자주 묻는 질문", "합류 여정")),
        ("카카오", ("인재풀 등록", "메인 메뉴")),
        ("우아한형제들", ("입사 후 혜택", "공고 검색")),
    ],
)
def test_the_source_text_leaves_the_page_furniture_out(
    site: str, unwanted: tuple[str, ...]
) -> None:
    """푸터의 주소나 GNB 문구가 원문에 있으면 근거 검사가 그것을 이 공고의 값으로 통과시킨다."""
    text = parsed(site).source_text

    for value in unwanted:
        assert value not in text, value


def test_the_kakao_source_text_drops_the_other_postings_beside_it() -> None:
    """카카오 상세는 컨테이너 안에 같은 직군의 다른 공고 열한 건을 담고 있다.

    열한 픽스처에서 페이지 부속 제거가 걸린 유일한 자리다. 부속을 빼지 않으면 옆 공고의
    직무와 근무지가 이 공고의 원문 안에 들어간다.
    """
    result = parsed("카카오")

    assert "조직소개" in result.source_text
    assert "SME 광고주 세일즈 및 컨설팅 담당자" not in result.source_text
    assert "제휴마케팅_어시스턴트" not in result.source_text


def test_the_body_node_is_used_as_is_when_its_parent_is_the_page() -> None:
    """부모가 `body` 면 페이지 전체가 원문이 된다. 그때는 본문 노드를 그대로 쓴다."""
    page = """
    <html><body>
      <nav>메뉴 하나 메뉴 둘</nav>
      <div class="post">본문이다</div>
      <footer>서울시 어딘가 대표전화</footer>
    </body></html>
    """
    text = source_text(BeautifulSoup(page, "html.parser"), ".post")

    assert "본문이다" in text
    assert "메뉴 하나" not in text
    assert "서울시 어딘가" not in text


def test_a_body_selector_that_matches_nothing_gives_no_source_text() -> None:
    """원문을 못 뽑는 것은 실패가 아니라 빈 값이다."""
    soup = BeautifulSoup("<html><body><div class='post'>본문</div></body></html>", "html.parser")

    assert source_text(soup, ".missing") == ""
    assert source_text(soup, "") == ""


# 상세가 API 인 네 곳
API_DETAIL: dict[str, str] = {
    "LG": "lg-detail-20260825.json",
    "한화": "hanwha-detail-20260825.json",
    "삼성": "samsung-detail-20260825.json",
    "현대자동차": "hyundai-detail-02800-20260825.json",
}


def api_source_text(site: str) -> str:
    payload = json.loads((FIXTURES / API_DETAIL[site]).read_text(encoding="utf-8"))
    config = validate_api_config(CONFIGS[site]["api_config"]).detail_config()
    result = build_detail(payload, config)
    assert result.fields["body"].strip()
    return result.source_text


@pytest.mark.parametrize("site", sorted(API_DETAIL))
def test_the_api_detail_source_text_is_text_and_not_markup(site: str) -> None:
    """LG 는 본문이 HTML 조각이다. 원문은 분류가 읽는 글이라 태그가 남으면 안 된다."""
    text = api_source_text(site)

    assert text.strip()
    assert not TAG.search(text), TAG.search(text)


@pytest.mark.parametrize(
    ("site", "wanted"),
    [
        # 본문 경로 밖에 있어 분류가 보지 못하던 값들
        ("한화", ["ruWorkpl: 본사(서울 63빌딩)", "ruRcrtPrsn: 총 0명"]),
        ("LG", ["[Finance]", "[HR]", "recEndDate: 2026.09.13 23:00"]),
        # 붙여 쓴 날짜와 시각은 코드값이 아니다
        ("현대자동차", ["applyEndDt: 20260830", "applyEndTm: 1705"]),
    ],
)
def test_the_api_detail_source_text_carries_what_the_body_path_left_out(
    site: str, wanted: list[str]
) -> None:
    text = api_source_text(site)

    for value in wanted:
        assert value in text


@pytest.mark.parametrize(
    ("site", "unwanted"),
    [
        ("한화", ["rtApplyHidnYn:", "rtSeq:", "www.hanwhain.com"]),
        ("LG", ["HR_REVIEW", "jobNoticeId:"]),
        # `fldCode: K0035` 는 빠지고 `fldCodeNm: 모빌리티 선행개발` 은 남는다
        ("현대자동차", ["recuNoticeSecretYn:", "fldCode:"]),
    ],
)
def test_the_api_detail_source_text_leaves_codes_and_links_out(
    site: str, unwanted: list[str]
) -> None:
    text = api_source_text(site)

    for value in unwanted:
        assert value not in text


def test_every_samsung_track_keeps_its_own_requirement() -> None:
    """겹친 문장은 직무 칸 안에서만 지운다. 공고 머리에 하나, 열두 직무에 하나씩이다.

    칸마다 한글(`qlfctKr`)과 영어(`qlfctEn`)에 같은 문장이 있어 지우지 않으면 스물여섯이다.
    """
    text = api_source_text("삼성")

    assert text.count("2년 이상 유관경력 보유하신 분") == 13
