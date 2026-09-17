"""상시 인재 풀 등록은 채용 공고가 아니다. 목록 제목으로 알아보고 상세를 열지 않는다.

2026-09-17 결정. LG 목록에 "[본사] 상시 인재 Pool 등록", "LG화학 상시 인재등록 공고",
"인재 상시 DB 등록용" 같은 항목이 섞여 들어와 공고로 쌓였다. 뽑는 자리가 정해지지 않은 이력서
접수라 오공고에 올리지 않는다.

띄어쓰기·대소문자를 지우고 글자 조각으로 본다. "인재 채용" 처럼 흔한 말은 넣지 않는다 — 진짜
공고가 빠지는 쪽이 인재 풀이 한 건 섞이는 쪽보다 나쁘다.
"""

from __future__ import annotations

import re

MARKERS: tuple[str, ...] = (
    "인재pool",
    "인재풀",
    "talentpool",
    "인재등록",
    "인재db",
    "db등록",
    "상시등록",
)


def is_talent_pool(title: str) -> bool:
    """제목이 인재 풀·인재 등록 접수인가. 제목이 비면 모른다 — 공고로 본다."""
    squashed = re.sub(r"\s+", "", title).lower()
    return any(marker in squashed for marker in MARKERS)
