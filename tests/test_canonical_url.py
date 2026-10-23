"""공고 주소에서 목록 위치를 뗀다 (LC-3504, 2026-10-23).

| 확인 | 깨지면 |
|---|---|
| 롯데 주소의 `?compcd=?id=bookmarkN` 이 떨어진다 | 같은 공고가 수집마다 새 공고로 들어간다 |
| 다른 파라미터와 나눈 공고 번호는 남는다 | 다른 공고가 한 주소로 합쳐진다 |
| 뗄 것이 없는 주소는 글자 그대로다 | 쌓인 다른 사이트 공고가 전부 새 공고가 된다 |
"""

from __future__ import annotations

import pytest

from app.crawler.canonical import canonical_url

LOTTE = "https://recruit.lotte.co.kr/apply/announcement/detail/21995366"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"{LOTTE}?compcd=?id=bookmark1001", LOTTE),
        (f"{LOTTE}?compcd=?id=bookmark10013", LOTTE),
        (f"{LOTTE}?compcd=?id=bookmark1001#2", f"{LOTTE}#2"),
        (f"{LOTTE}?compcd=%3Fid%3Dbookmark1001", LOTTE),
        (f"{LOTTE}?lang=ko&compcd=?id=bookmark1001", f"{LOTTE}?lang=ko"),
    ],
)
def test_the_list_position_comes_off(url: str, expected: str) -> None:
    assert canonical_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        LOTTE,
        "https://job.alio.go.kr/recruitview.do?idx=306026",
        "https://www.work24.go.kr/wk/a/b/1500/empDetailAuthView.do?wantedAuthNo=K1&infoTypeCd=V",
        "https://x.test/view?q=a+b&id=bookmarks",
        "https://x.test/view?id=7#bookmark1001",
    ],
)
def test_other_urls_stay_as_they_are(url: str) -> None:
    assert canonical_url(url) == url
