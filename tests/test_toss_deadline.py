"""토스 마감일 셀렉터가 인용문이 아니라 모집 기간을 잡는지 본다 (1.1.V).

`blockquote` 하나였을 때 264건 중 138건이 인용문을 마감일로 받았다. 값이 날짜가 아니라
`recruitment_end_at` 의 `date_parse` 규칙이 실패했고, 그 실패는 필드 하나가 비는 것으로 끝나지 않고
공고를 통째로 정규화에서 떨어뜨린다 (`app/normalize/engine.py`). 264건 중 126건만
`normalized_jobs` 에 들어간 것이 그 결과다.

토스 상세에서 마감일을 말하는 자리는 `모집 기간` 을 이름표로 단 `blockquote` 하나뿐이고,
264건 중 본문에 그 글자가 있는 것은 4건이다. 나머지는 마감일이 없는 공고다 — 빈 칸이 맞는
답이고, 인용문을 대신 넣는 것이 틀린 답이다.

픽스처 두 건으로 양쪽을 다 본다. 실사이트에 나가지 않는다.

| 픽스처 | 모집 기간 blockquote | 기대 |
|---|---|---|
| `toss-detail-7827417003-20260826.html` | 있다 | 마감일 문장을 읽는다 |
| `toss-detail-5853067003-20260826.html` | 없다 | 빈 칸이다. 인용문을 읽지 않는다 |
"""

from __future__ import annotations

import json
import pathlib

from app.crawler.parser import parse_detail
from app.selector.schema import SelectorSet, validate_selectors

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
SEEDS = pathlib.Path(__file__).parent.parent / "seeds" / "site-configs-20260826.json"

# 모집 기간을 단 blockquote 가 있는 공고와 없는 공고
WITH_PERIOD = "toss-detail-7827417003-20260826.html"
WITHOUT_PERIOD = "toss-detail-5853067003-20260826.html"


def toss_selectors() -> SelectorSet:
    """`seeds/site-configs-20260826.json` 에 적힌 그대로. 여기서 다시 쓰지 않는다."""
    entries = json.loads(SEEDS.read_text(encoding="utf-8"))["crawlers"]
    entry = next(item for item in entries if item["name"] == "토스")
    return validate_selectors(entry["selectors"])


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def deadline_of(name: str) -> str:
    return parse_detail(read(name), toss_selectors().detail).fields["recruitment_end_at"]


def test_the_deadline_selector_asks_for_the_period_label() -> None:
    """`blockquote` 하나로는 어느 인용문이 먼저 오는지에 값이 걸린다."""
    selector = toss_selectors().detail.recruitment_end_at

    assert selector != "blockquote"
    assert "모집 기간" in selector


def test_a_posting_that_states_its_period_gives_the_date() -> None:
    value = deadline_of(WITH_PERIOD)

    assert "26년 8월 31일" in value
    assert "동료의 한마디" not in value


def test_a_posting_without_a_period_gives_an_empty_deadline() -> None:
    """이 공고에도 `blockquote` 는 있다. 옛 셀렉터는 그 인용문을 마감일로 받았다."""
    assert "blockquote" in read(WITHOUT_PERIOD)

    assert deadline_of(WITHOUT_PERIOD) == ""


def test_the_body_still_reads_on_both_shapes() -> None:
    """마감일을 좁힌 것이 본문을 건드리지 않았는지 본다."""
    selectors = toss_selectors()

    for name in (WITH_PERIOD, WITHOUT_PERIOD):
        fields = parse_detail(read(name), selectors.detail).fields
        assert len(fields["body"]) > 1000, name
        assert fields["title"].strip(), name
