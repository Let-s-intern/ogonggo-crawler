"""목록에서 읽은 공고 링크가 넣은 예시 공고 주소와 같은 모양인가.

11/16 일괄 추가를 사이트별로 다시 돌려 본 결과(2026-11-24, LC-3654), 목록 링크와 예시 공고 주소의
모양이 다를 때는 둘 중 하나가 틀려 있었다.

- 휴온스: 목록 링크 `/career/jobs/134345`, 예시 `/app/jobnotice/view?…&jobnoticeSn=…`.
  틀린 쪽은 예시다. 사이트가 새 채용 화면으로 옮겼다
- 데상트: 목록 링크 `/ko/products`, 예시 `/ko/o/189424`. 틀린 쪽은 목록이다. 상품 소개
  카드를 항목으로 골랐다
- 네오위즈: 목록 링크 `/kr/investor/announcement/259`, 예시 `/kr/career/browse-job/<uuid>`.
  틀린 쪽은 목록이다. 메뉴의 투자자 공시로 옮겨 갔다

모양은 호스트, 경로 칸 수, 번호·식별자가 아닌 칸, 쿼리 이름이다. 번호와 식별자, 쿼리 값은 공고마다
다르므로 보지 않는다.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

# 공고마다 달라지는 경로 칸. 번호, 긴 16진수·UUID, 8자 이상 식별자 중 숫자가 섞였거나
# 대소문자가 섞인 것(나인하이어 `GBDlUZTI`·`oz6xlsi9`). `browse-job` 같은 낱말은 아니다
# 낱말 뒤에 순번만 붙은 칸(`products1`·`blog12`·`notice5`)은 식별자가 아니라 페이지 이름이다 —
# 데상트의 상품 소개 `/ko/products1` 을 공고로 보고 직무 소개를 공고로 등록했다 (2026-11-24)
_ID = (
    r"(\d+|[0-9a-f-]{16,}|(?![A-Za-z]+[-_]?\d{1,3}$)(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{8,}"
    r"|(?=\w*[a-z])(?=\w*[A-Z])[A-Za-z0-9]{8,})"
)
_ID_SEGMENT = re.compile(f"^{_ID}$")
# 공고 한 건 주소의 끝 칸. 목록 링크가 공고처럼 보이는지 가를 때 쓴다
_POSTING_END = re.compile(f"/{_ID}/?$")

Shape = tuple[str, tuple[str, ...], tuple[str, ...]]


def shape(url: str) -> Shape:
    """주소의 모양. 호스트(`www.` 뺌), 경로 칸(식별자는 `*`), 쿼리 이름."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    segments = tuple(
        "*" if _ID_SEGMENT.match(segment) else segment.lower()
        for segment in parts.path.split("/")
        if segment
    )
    # 값이 빈 쿼리는 보지 않는다. 서울우유 예시는 `&search=&keyword=` 를 달고, 목록 링크는
    # `&search=` 만 단다
    keys = tuple(sorted({key.lower() for key, value in parse_qsl(parts.query) if value}))
    return host, segments, keys


def matches_sample(links: list[str], sample: str) -> bool:
    """링크 중 하나라도 예시 공고와 모양이 같은가. 견줄 것이 없으면 같은 것으로 본다."""
    if not sample.strip() or not links:
        return True
    wanted = shape(sample)
    return any(shape(link) == wanted for link in links)


def looks_like_posting(url: str) -> bool:
    """공고 한 건 주소처럼 보이는가. 끝 칸이 번호나 식별자이거나, 쿼리 값에 번호가 있다."""
    parts = urlsplit(url.strip())
    if _POSTING_END.search(parts.path):
        return True
    return any(value.isdigit() for _, value in parse_qsl(parts.query))
