"""직무가 제목에 있는지 본문에 있는지 열한 사이트 픽스처로 잰다 (2.1.V).

`position_name` 은 제목에서 뽑는 자유 텍스트다 (`.claude/tasks/todo/prd-fields-and-logo.md`).
뽑는 칸이라 근거 검사를 지나야 하는데, 그 검사가 지금은 본문에만 값을 돌려 본다
(`app/classify/grounding.py`). **제목에서 뽑은 값을 본문에만 돌려 보면 맞게 뽑은 값이 통째로
버려진다.** 얼마나 버려지는지가 근거 검사에 제목을 더할지를 정하므로 그것을 여기서 잰다.

## 잰 것 (2026-08-28, `tests/fixtures/` 의 상세 열한 건)

| 사이트 | 제목이 말하는 직무 | 제목 | 본문 |
|---|---|---|---|
| 한화 | LIFEPLUS TV 마케팅 기획 및 운영 | 있다 | 있다 |
| 롯데그룹 | 전기 | 있다 | 있다 |
| 두산 | 광고영업 | 있다 | 있다 |
| 삼성 | R&D분야 | 있다 | 없다 |
| 현대자동차 | 항공용 전기추진시스템 고장진단 SW 개발 | 있다 | 없다 |
| SK | Global IT 통합 및 기획 | 있다 | 없다 |
| 네이버 | 의료 도메인에서의 Agentic RAG 연구 및 개발 | 있다 | 없다 |
| 카카오 | 카카오비즈니스 파트너 플랫폼 PM | 있다 | 없다 |
| 우아한형제들 | 파트너 영업 Specialist | 있다 | 없다 |
| LG | 없다 | 해당없음 | 해당없음 |
| 토스 | 없다 | 해당없음 | 해당없음 |

제목이 직무를 말하는 곳이 아홉, 그중 본문에서도 같은 글자를 찾는 곳이 셋, **제목에만 있는
곳이 여섯**이다. 나머지 둘(LG·토스)은 여러 직무를 한 공고에 묶은 통합 공고라 제목이 직무를
말하지 않는다 — 그런 공고의 `position_name` 은 빈 칸이 맞다.

## 이 숫자가 정한 것

근거 검사의 대상에 **제목을 더한다.** 본문에만 돌려 보면 제목이 직무를 말하는 아홉 중 여섯이
버려진다. 셋 중 둘이다.

SK 는 경계에 있다. 본문에 `Global IT통합` 은 있고 `Global IT 통합 및 기획` 은 없다. 제목이
말하는 직무를 그대로 옮기면 버려지고, 짧게 잘라 옮기면 남는다 — 모델에게 어디까지 자르라고
시킬 수 있는 종류의 일이 아니라 제목에만 있는 쪽으로 센다.

## 재는 법

`missing_lines` 를 그대로 쓴다 (`app/classify/grounding.py`). 눈으로 "제목에 있다" 를 세면
근거 검사가 실제로 무엇을 통과시키는지와 어긋난다 — 공백과 글머리표를 걷어낸 뒤의 비교가
판정이고, 이 표는 그 판정을 그대로 옮긴 것이라야 뜻이 있다.

실사이트에 나가지 않는다. 열한 사이트의 응답이 전부 `tests/fixtures/` 에 있다.
"""

from __future__ import annotations

import pytest

from app.classify.grounding import in_body
from tests.test_split_body_mapping import SITES, collected

# 제목이 말하는 직무. 제목에 적힌 글자를 그대로 옮겼고, 제목이 직무를 말하지 않는 곳은 빈
# 문자열이다 — 통합 공고의 `position_name` 은 빈 칸이 맞다
TITLE_ROLE: dict[str, str] = {
    "LG": "",
    "한화": "LIFEPLUS TV 마케팅 기획 및 운영",
    "삼성": "R&D분야",
    "현대자동차": "항공용 전기추진시스템 고장진단 SW 개발",
    "SK": "Global IT 통합 및 기획",
    "롯데그룹": "전기",
    "두산": "광고영업",
    "네이버": "의료 도메인에서의 Agentic RAG 연구 및 개발",
    "토스": "",
    "카카오": "카카오비즈니스 파트너 플랫폼 PM",
    "우아한형제들": "파트너 영업 Specialist",
}

# 그 직무를 본문에서도 그대로 찾을 수 있는 사이트. 나머지는 제목에만 있다
ALSO_IN_BODY: frozenset[str] = frozenset({"한화", "롯데그룹", "두산"})

# 제목이 직무를 말하는 사이트
NAMED = tuple(site for site in SITES if TITLE_ROLE[site])


def test_the_table_covers_every_site() -> None:
    """픽스처가 늘면 표도 늘어야 한다. 빠진 사이트는 조용히 안 세어진다."""
    assert set(TITLE_ROLE) == set(SITES)
    assert ALSO_IN_BODY <= set(NAMED)


@pytest.mark.parametrize("site", NAMED)
def test_the_role_the_title_names_is_in_the_title(site: str) -> None:
    """표에 적은 직무가 정말 제목에서 나온 글자인지. 아니면 셈의 근거가 사라진다."""
    values = collected(site)

    assert in_body(TITLE_ROLE[site], values["title"]), site


@pytest.mark.parametrize("site", NAMED)
def test_whether_the_body_repeats_the_role(site: str) -> None:
    """본문이 같은 글자를 되풀이하는지. 표의 마지막 열이 이 판정이다."""
    values = collected(site)

    assert in_body(TITLE_ROLE[site], values["body"]) is (site in ALSO_IN_BODY), site


def test_six_of_the_nine_would_be_dropped_by_a_body_only_check() -> None:
    """본문에만 돌려 보면 아홉 중 여섯이 버려진다. 이 숫자가 2.4 를 정했다."""
    dropped = [site for site in NAMED if not in_body(TITLE_ROLE[site], collected(site)["body"])]

    assert len(NAMED) == 9
    assert sorted(dropped) == sorted(
        ["삼성", "현대자동차", "SK", "네이버", "카카오", "우아한형제들"]
    )


def test_the_two_bundled_postings_name_no_role() -> None:
    """LG 와 토스는 여러 직무를 한 공고에 묶었다. 그 둘의 직무는 빈 칸이 맞다."""
    assert [site for site in SITES if not TITLE_ROLE[site]] == ["LG", "토스"]
