"""넣은 주소에서 공고 목록을 못 읽으면 메뉴의 목록 링크를 따라가 다시 만든다.

11/16 일괄 추가(2026-11-22, LC-3654)의 실패에서 나왔다. 채용 사이트 첫 화면에는 목록이 없거나
(목록 없음), 링크 없는 공고 카드·인터뷰 카드가 반복으로 잡힌다(링크 못 읽음). 생성도 실사이트도
부르지 않는다. `_generate` 와 메뉴 후보를 갈아끼우고 어느 주소로 만들었는지만 본다.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.api import crawlers as crawlers_api
from app.crawler.playwright import STATIC
from app.selector.generator import GenerationResult, Usage
from app.selector.list_page import Candidate
from app.selector.schema import validate_selectors

HOME = "https://team.example.test/"
RECRUIT = "https://team.example.test/recruit"

SELECTORS: dict[str, Any] = {
    "list": {"item": "li", "title": "a", "link": "a", "date": ""},
    "detail": {
        "title": "h1",
        "body": "div.body",
        "qualifications": "",
        "recruitment_end_at": "",
        "department": "",
    },
}


class Report:
    def __init__(self, failed_list_fields: list[str]) -> None:
        self.failed_list_fields = failed_list_fields
        self.list_missing = "list.item" in failed_list_fields
        self.list_fields_missing = False
        self.failed = list(failed_list_fields)
        self.skipped: list[str] = []

    def summary(self) -> dict[str, int]:
        return {"list.item": 0 if self.list_missing else 5}


def _result(failed: list[str]) -> GenerationResult:
    return GenerationResult(
        selectors=validate_selectors(SELECTORS),
        usage=Usage(
            provider="gemini",
            model="m",
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_ms=1,
        ),
        attempts=1,
        verification=Report(failed),  # type: ignore[arg-type]
    )


def _install(
    monkeypatch: pytest.MonkeyPatch, home_failed: list[str], candidate: Candidate
) -> list[str]:
    """`_generate` 가 불린 목록 주소가 쌓인다. 첫 화면은 `home_failed`, 목록 페이지는 성공이다."""
    called: list[str] = []

    async def generate(
        list_url: str, detail_url: str, render_mode: str, settings: Any = None
    ) -> GenerationResult:
        called.append(list_url)
        return _result(home_failed if list_url == HOME else [])

    async def links(list_url: str, render_mode: str) -> list[Candidate]:
        return [candidate]

    monkeypatch.setattr(crawlers_api, "_generate", generate)
    monkeypatch.setattr(crawlers_api, "_list_page_links", links)
    return called


@pytest.mark.asyncio
async def test_목록이_없으면_메뉴_링크를_따라간다(monkeypatch: pytest.MonkeyPatch) -> None:
    called = _install(monkeypatch, ["list.item"], Candidate(RECRUIT, "채용", 1))

    result = await crawlers_api.get_generator(None)(HOME, "", "")

    assert called[-1] == RECRUIT
    assert result.list_url == RECRUIT


@pytest.mark.asyncio
async def test_링크를_못_읽으면_확실한_메뉴_링크만_따라간다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """월부: 회사 소개의 인터뷰 카드를 반복으로 잡았고, 공고는 메뉴의 "채용공고" 페이지에 있다."""
    called = _install(monkeypatch, ["list.link"], Candidate(RECRUIT, "채용공고", 5))

    result = await crawlers_api.get_generator(None)(HOME, "", "")

    assert called[-1] == RECRUIT
    assert result.list_url == RECRUIT
    assert any("공고로 가는 링크를 읽지 못해" in note for note in result.notes)


@pytest.mark.asyncio
async def test_링크만_못_읽었고_메뉴_글자가_약하면_그대로_둔다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """항목을 눌러 상세를 찾는 목록일 수 있다(CJ). "채용" 한 단어 링크로는 옮기지 않는다."""
    called = _install(monkeypatch, ["list.link"], Candidate(RECRUIT, "채용", 1))

    result = await crawlers_api.get_generator(None)(HOME, "", "")

    assert RECRUIT not in called
    assert result.list_url == ""


@pytest.mark.asyncio
async def test_모드를_고른_등록은_따라가지_않는다(monkeypatch: pytest.MonkeyPatch) -> None:
    called = _install(monkeypatch, ["list.item"], Candidate(RECRUIT, "채용공고", 5))

    await crawlers_api.get_generator(None)(HOME, "", STATIC)

    assert called == [HOME]


@pytest.mark.asyncio
async def test_옮긴_페이지의_링크가_예시와_모양이_다르면_버린다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """네오위즈(2026-11-24): 메뉴의 "공고" 가 투자자 공시 게시판이었다."""
    from dataclasses import replace

    _install(monkeypatch, ["list.link"], Candidate(RECRUIT, "채용공고", 5))
    original = crawlers_api._generate

    async def generate(
        list_url: str, detail_url: str, render_mode: str, settings: Any = None
    ) -> GenerationResult:
        result = await original(list_url, detail_url, render_mode, settings)
        return replace(result, list_links=("https://team.example.test/investor/notice/259",))

    monkeypatch.setattr(crawlers_api, "_generate", generate)

    result = await crawlers_api.get_generator(None)(
        HOME, "https://team.example.test/career/job/c977653e-1360-4c18-a48b-452af618f650", ""
    )

    assert result.list_url == ""


def test_목록_링크가_공고_주소인데_예시와_모양이_다르면_첫_공고로_바꾼다() -> None:
    """휴온스(2026-11-24): 예시는 옛 채용 화면, 목록 링크는 새 화면이었다."""
    from dataclasses import replace

    old = "https://h.recruiter.co.kr/app/jobnotice/view?systemKindCode=MRS2&jobnoticeSn=1"
    new = "https://h.recruiter.co.kr/career/jobs/134345"
    result = _result([])

    assert crawlers_api._stale_sample(old, replace(result, list_links=(new,))) == new
    # 모양이 같거나, 공고 주소처럼 보이지 않거나, 메뉴로 옮긴 목록이면 그대로 둔다
    assert crawlers_api._stale_sample(new, replace(result, list_links=(new,))) == ""
    products = "https://h.recruiter.co.kr/ko/products"
    assert crawlers_api._stale_sample(old, replace(result, list_links=(products,))) == ""
    moved = replace(result, list_links=(new,), list_url=RECRUIT)
    assert crawlers_api._stale_sample(old, moved) == ""
