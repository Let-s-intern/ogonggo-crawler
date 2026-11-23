"""넣은 주소에 목록이 없을 때, 그 페이지 메뉴에서 공고 목록 페이지로 가는 링크를 고른다.

11/16 일괄 추가(2026-11-22, LC-3654)에서 목록을 못 찾은 24곳 대부분이 채용 사이트의 첫 화면이나
회사 소개 페이지였다. 그리팅·나인하이어로 만든 채용 사이트는 첫 화면(`/ko/home`, `/ko/intro`)이
회사 소개이고 공고는 메뉴의 "채용 공고" 페이지(`/ko/apply`, `/careers`)에 있다. 엑셀을 채우는
사람은 첫 화면 주소를 적는다. 사람이 메뉴를 한 번 누르면 되는 일이라 등록도 한 번만 따라간다.

고르는 것은 글자와 경로다. 따라가 본 페이지에서 목록이 나와야 그 주소로 등록한다 — 고르기가
틀려도 등록이 엉뚱한 페이지로 끝나지 않는다. 비용(셀렉터 생성 한 번)이 있어 후보는 둘까지다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

# 따라가 볼 후보 수. 하나마다 셀렉터 생성을 한 번 더 부른다
MAX_CANDIDATES = 2

# 공고 목록을 가리키는 글자. 메뉴 이름과 "보러가기" 버튼이 이렇게 쓴다
_STRONG_TEXT = re.compile(
    r"채용\s*공고|채용\s*중|진행\s*중|공고\s*(보러|보기|바로)|채용\s*바로\s*가기|모집\s*중|"
    r"open\s*positions?|job\s*openings?|current\s*openings?|all\s*jobs",
    re.IGNORECASE,
)
_WEAK_TEXT = re.compile(r"채용|공고|지원\s*하기|입사\s*지원|합류|careers?|jobs?|recruit", re.I)
_PATH = re.compile(
    r"(^|/)(apply|jobs?|careers?|recruit[\w-]*|positions?|openings?|announcement|job_posting)"
    r"(/|$)",
    re.IGNORECASE,
)
# 공고 목록이 아닌 것이 확실한 페이지. 글자가 "채용" 을 담아도 건너뛴다
_NOT_LIST = re.compile(
    r"faq|privacy|policy|개인정보|약관|login|sign-in|로그인|합격자|문의|culture|benefit|복리|"
    r"interview|blog|뉴스",
    re.IGNORECASE,
)
# 공고 한 건 주소. 목록이 아니라 그 안의 항목이다 — 끝 칸이 번호나 긴 식별자다 (그리팅
# `/ko/o/123`, `/jobs/1`, 나인하이어 `/job_posting/GBDlUZTI` 는 경로만 보고 거르지 못한다)
_POSTING = re.compile(r"/(\d+|[0-9a-f-]{20,})/?$", re.IGNORECASE)


@dataclass(frozen=True)
class Candidate:
    url: str
    text: str
    score: int


def list_page_candidates(html: str, page_url: str) -> list[Candidate]:
    """`page_url` 페이지에서 공고 목록 페이지일 법한 같은 호스트 링크를, 그럴듯한 순으로."""
    here = urlsplit(page_url)
    best: dict[str, Candidate] = {}
    for anchor in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        href = str(anchor["href"]).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        url = urljoin(page_url, href).split("#", 1)[0]
        parts = urlsplit(url)
        if parts.netloc.lower() != here.netloc.lower():
            continue
        if parts.path.rstrip("/") == here.path.rstrip("/") and parts.query == here.query:
            continue
        if _POSTING.search(parts.path):
            continue
        text = " ".join(anchor.get_text(" ", strip=True).split())[:40]
        score = _score(text, parts.path)
        if score <= 0:
            continue
        if url not in best or best[url].score < score:
            best[url] = Candidate(url=url, text=text, score=score)
    ranked = sorted(best.values(), key=lambda item: -item.score)
    return ranked[:MAX_CANDIDATES]


def _score(text: str, path: str) -> int:
    if _NOT_LIST.search(text) or _NOT_LIST.search(path):
        return 0
    score = 0
    if _STRONG_TEXT.search(text):
        score += 3
    elif _WEAK_TEXT.search(text):
        score += 1
    if _PATH.search(path):
        score += 2
    return score
