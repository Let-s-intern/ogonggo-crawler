"""공고 주소에서 목록 위치처럼 실행마다 바뀌는 조각을 뗀다 (2026-10-23, LC-3504).

수집·전송·오공고가 모두 주소 하나로 같은 공고인지 가린다 (`app/crawler/runner.py` 의 `_is_known`,
`app/deliver/spring.py`). 그런데 롯데는 목록 링크에 그 항목의 목록 위치를 넣는다.

    /apply/announcement/detail/21995366?compcd=?id=bookmark1001

`bookmark1001` 은 즐겨찾기 버튼의 id 이고 `1000 + 목록 순번` 이다. 새 공고가 위에 붙으면 순번이
밀려 같은 공고의 주소가 바뀌고, 그때마다 새 공고로 적재돼 오공고에 4~5번씩 들어갔다. 상세는 이
조각 없이도 같은 페이지를 준다.

떼는 것은 `_POSITION` 에 든 모양뿐이다. 뗄 것이 없는 주소는 받은 글자 그대로 돌려준다 — 파라미터
순서나 인코딩을 다시 쓰면 이미 쌓인 다른 사이트 공고의 주소가 바뀌어 전부 새 공고가 된다.
"""

from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit, urlunsplit

# 쿼리 조각(`이름=값`) 하나가 목록 위치를 담았는가. 롯데는 `compcd=?id=bookmark1001` 처럼 `compcd`
# 값 안에 `?id=` 가 한 번 더 들어 있다
_POSITION = re.compile(r"^(?:compcd=\??)?id=bookmark\d+$")


def canonical_url(url: str) -> str:
    """같은 공고가 늘 같은 값이 되는 주소. 뗄 조각이 없으면 `url` 그대로다."""
    parts = urlsplit(url)
    if not parts.query:
        return url
    pieces = parts.query.split("&")
    kept = [piece for piece in pieces if not _POSITION.match(unquote(piece))]
    if len(kept) == len(pieces):
        return url
    return urlunsplit(parts._replace(query="&".join(kept)))
