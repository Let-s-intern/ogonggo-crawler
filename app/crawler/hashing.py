"""중복 감지용 content hash.

해시에 들어가는 필드는 `.claude/docs/data-model.md` 의 "중복 감지 hash" 절이 정한다.
`source_url`, `title`, `recruitment_end_at`, `body` 넷뿐이고, 그 밖의 키는 무엇이 들어오든 무시한다.

조회수, "3일 전" 같은 상대 날짜, 광고 문구, 정렬 순서, 크롤링 시각이 하나라도 섞이면 매 크롤마다
값이 달라져 같은 공고가 매번 신규로 적재된다. 그래서 넣을 것을 고르는 대신 뺄 것을 무시하는
방식이 아니라, 넣을 것만 이름으로 집는다.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

# 순서가 해시의 일부다. 바꾸면 기존 값과 맞지 않는다.
HASH_FIELDS: tuple[str, ...] = ("source_url", "title", "recruitment_end_at", "body")

# 추출된 텍스트에 나올 일이 없는 구분자. 필드 경계가 값 안에서 흉내내지지 않게 한다.
_FIELD_SEPARATOR = "\x1f"

_WHITESPACE = re.compile(r"\s+")


def content_hash(raw: Mapping[str, Any]) -> str:
    """`raw_jobs.content_hash` 에 넣을 값을 만든다.

    `raw` 는 셀렉터로 뽑은 필드 묶음이다. `HASH_FIELDS` 밖의 키는 읽지 않는다.
    """
    joined = _FIELD_SEPARATOR.join(_text(raw.get(name)) for name in HASH_FIELDS)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _text(value: Any) -> str:
    """해시에 넣을 문자열로 만든다.

    없는 값은 빈 문자열이다. 앞뒤 공백과 줄바꿈·연속 공백의 차이는 흡수한다 — 같은 공고가 공백
    하나 때문에 신규가 되지 않게 하려는 것이고, 값의 의미를 바꾸는 정제는 여기서 하지 않는다.
    """
    if value is None:
        return ""
    return _WHITESPACE.sub(" ", str(value)).strip()
