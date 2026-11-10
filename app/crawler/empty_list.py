"""목록이 0건일 때 "공고가 정말 없다" 고 페이지가 말하는지 본다 (2026-11-10 결정).

목록 0건은 실패다 (`CLAUDE.md`). 그런데 채용공고가 하나도 없는 회사가 꽤 있고, 그 사이트가 매번
`고치기` 로 뜨면 정말 고칠 사이트가 묻힌다. 그래서 **페이지가 빈 목록이라고 직접 말할 때만** 실패가
아닌 `공고 없음` 으로 둔다.

| 신호 | 판정 |
|---|---|
| HTML 에 `진행 중인 채용이 없습니다` · `No open positions` 같은 빈 목록 문구가 보인다 | 공고 없음 |
| API 응답에 `items_path` 배열은 있는데 비었다 | 공고 없음 |
| 신호가 없다 | 지금처럼 실패 (`selector_miss`) |

문구는 보이는 글자에서만 찾는다. `<script>`·`<template>` 안의 문구는 화면에 없는 것이라, 거기서
찾으면 자바스크립트로 그리는 사이트가 늘 `공고 없음` 으로 보인다.

사이트를 처음 찾는 경로(`app/selector`)는 이 판정을 쓰지 않는다. 그쪽에서 빈 목록을 성공으로
받으면 엉뚱한 셀렉터를 맞았다고 저장한다 — 그래서 예외는 `SelectorMissError` 의 하위 클래스이고,
러너만 따로 받는다.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup

# "공고가 없습니다" · "진행중인 채용이 없어요" · "등록된 게시물이 없습니다" 같은 꼴. 주어 뒤에
# 조사 하나를 두고 `없` 이 온다
_KOREAN = re.compile(
    r"(?:채용\s*공고|공고|포지션|채용|모집|게시물|게시글|검색\s*결과|조회\s*결과|데이터|직무)"
    r"\s*(?:이|가|은|는)\s*(?:현재\s*|아직\s*)?없"
)
_ENGLISH = re.compile(
    r"\bno\s+(?:current\s+|open\s+|available\s+)?"
    r"(?:open\s+)?(?:positions?|openings?|jobs?|vacancies|roles?|results?)\b"
    r"|\bthere\s+are\s+(?:currently\s+)?no\b",
    re.IGNORECASE,
)
# 보이지 않는 글자를 담는 태그. 여기 든 문구는 화면에 없다
_HIDDEN_TAGS = ("script", "style", "template", "noscript")
# 사유에 적는 문구 앞뒤 글자 수
_CONTEXT = 20


def empty_notice(html: str) -> str:
    """페이지가 빈 목록이라고 말하는 문구. 없으면 빈 문자열이다."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(_HIDDEN_TAGS):
        tag.decompose()
    text = " ".join(soup.get_text(" ").split())
    found = _KOREAN.search(text) or _ENGLISH.search(text)
    if found is None:
        return ""
    start = max(found.start() - _CONTEXT, 0)
    return text[start : found.end() + _CONTEXT].strip()
