"""정적으로 목록이 안 잡히면 등록이 스스로 렌더로 올려 다시 만든다.

운영자에게 모드를 묻지 않기로 했으므로(`app/api/ui_crawlers.py`), 정적 HTML 이 껍데기인
사이트에서 등록이 거기서 멈추면 목록 URL 하나로 등록이 끝나지 않는다. 카카오 목록이 정적으로
껍데기 1,553B 다.

Gemini 도 실사이트도 브라우저도 부르지 않는다. `open_source` 와 생성 함수를 갈아끼우고,
확인하는 것은 어느 모드로 몇 번 만들었는가와 결과에 어느 경로가 적혔는가다.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.api import crawlers as crawlers_api
from app.crawler.fetcher import FetchResult
from app.crawler.playwright import PLAYWRIGHT, STATIC, RenderUnavailableError
from app.selector.generator import GenerationResult, Usage
from app.selector.schema import SelectorSet, validate_selectors

LIST_URL = "https://careers.example.test/jobs"
DETAIL_URL = "https://careers.example.test/jobs/1"

SHELL = "<html><body><div id='root'></div></body></html>"
RENDERED = (
    "<html><body><ul><li class='card'><a href='/jobs/1'>공고 하나</a></li></ul></body></html>"
)

SELECTORS: dict[str, Any] = {
    "list": {"item": "li.card", "title": "a", "link": "a", "date": "span"},
    "detail": {
        "title": "h1",
        "body": "div.body",
        "qualifications": "",
        "recruitment_end_at": "",
        "department": "",
    },
}


class Report:
    """검증 결과 대역. 목록을 찾았는지만 답한다."""

    def __init__(self, list_missing: bool) -> None:
        self.list_missing = list_missing
        self.list_fields_missing = False
        self.failed: list[str] = []
        self.failed_list_fields: list[str] = ["list.item"] if list_missing else []
        self.skipped: list[str] = []

    def summary(self) -> dict[str, int]:
        return {"list.item": 0 if self.list_missing else self.items}

    items = 3


class Source:
    """`open_source` 가 돌려주는 대역. 모드마다 다른 HTML 을 준다."""

    def __init__(self, mode: str) -> None:
        self._mode = mode

    async def fetch(self, url: str) -> FetchResult:
        text = RENDERED if self._mode == PLAYWRIGHT else SHELL
        return FetchResult(url=url, status_code=200, text=text)


def result_for(html: str) -> GenerationResult:
    return GenerationResult(
        selectors=validate_selectors(SELECTORS),
        usage=Usage(
            provider="gemini",
            model="gemini-3.5-flash",
            input_tokens=10,
            output_tokens=10,
            total_tokens=20,
            latency_ms=10,
        ),
        attempts=1,
        verification=Report(list_missing="card" not in html),  # type: ignore[arg-type]
    )


@pytest.fixture
def modes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """생성이 어떤 모드로 불렸는지 순서대로 쌓인다."""
    called: list[str] = []

    @asynccontextmanager
    async def open_source(render_mode: str, fetcher: Any, **kwargs: Any) -> AsyncIterator[Source]:
        called.append(render_mode)
        yield Source(render_mode)

    async def generate_for_urls(
        list_url: str, detail_url: str, *, source: Any, **kwargs: Any
    ) -> GenerationResult:
        return result_for((await source.fetch(list_url)).text)

    monkeypatch.setattr(crawlers_api, "open_source", open_source)
    monkeypatch.setattr(crawlers_api, "generate_for_urls", generate_for_urls)
    monkeypatch.setattr(crawlers_api, "get_fetcher", lambda: None)
    return called


@pytest.mark.asyncio
async def test_정적_껍데기면_렌더로_다시_만든다(modes: list[str]) -> None:
    generate = crawlers_api.get_generator(None)

    result = await generate(LIST_URL, DETAIL_URL, "")

    assert modes == [STATIC, PLAYWRIGHT]
    assert result.render_mode == PLAYWRIGHT
    assert result.verification.list_missing is False
    assert "정적 HTML 에는 목록이 없어 렌더한 HTML 로 셀렉터를 만들었다" in result.notes


@pytest.mark.asyncio
async def test_정적으로_목록이_잡히면_브라우저를_띄우지_않는다(
    monkeypatch: pytest.MonkeyPatch, modes: list[str]
) -> None:
    """렌더 한 번이 정적 fetch 의 몇십 배다. 될 때는 열지 않는다."""

    async def generate_for_urls(
        list_url: str, detail_url: str, *, source: Any, **kwargs: Any
    ) -> GenerationResult:
        return result_for(RENDERED)

    monkeypatch.setattr(crawlers_api, "generate_for_urls", generate_for_urls)
    generate = crawlers_api.get_generator(None)

    result = await generate(LIST_URL, DETAIL_URL, "")

    assert modes == [STATIC]
    assert result.render_mode == STATIC
    assert result.notes == []


@pytest.mark.asyncio
async def test_모드를_고른_등록은_그대로_한_번만_만든다(modes: list[str]) -> None:
    """고른 값을 판정이 덮어쓰지 않는다 (`.claude/rules/llm.md`)."""
    generate = crawlers_api.get_generator(None)

    result = await generate(LIST_URL, DETAIL_URL, STATIC)

    assert modes == [STATIC]
    assert result.render_mode == STATIC
    assert result.verification.list_missing is True


@pytest.mark.asyncio
async def test_렌더로도_목록이_없으면_렌더한_결과를_올린다(
    monkeypatch: pytest.MonkeyPatch, modes: list[str]
) -> None:
    """정적으로도 렌더로도 없었다는 사실이 사유에 남아야 다음 수단이 갈린다."""

    async def generate_for_urls(
        list_url: str, detail_url: str, *, source: Any, **kwargs: Any
    ) -> GenerationResult:
        return result_for(SHELL)

    monkeypatch.setattr(crawlers_api, "generate_for_urls", generate_for_urls)
    generate = crawlers_api.get_generator(None)

    result = await generate(LIST_URL, DETAIL_URL, "")

    assert modes == [STATIC, PLAYWRIGHT]
    assert result.render_mode == PLAYWRIGHT
    assert result.verification.list_missing is True


@pytest.mark.asyncio
async def test_브라우저가_없으면_정적_결과를_사유와_함께_올린다(
    monkeypatch: pytest.MonkeyPatch, modes: list[str]
) -> None:
    """렌더가 안 되는 배포에서 실패 사유가 렌더 쪽으로 바뀌면 운영자가 엉뚱한 곳을 본다."""

    async def generate_for_urls(
        list_url: str, detail_url: str, *, source: Any, **kwargs: Any
    ) -> GenerationResult:
        if modes[-1] == PLAYWRIGHT:
            raise RenderUnavailableError("playwright 패키지가 없다")
        return result_for(SHELL)

    monkeypatch.setattr(crawlers_api, "generate_for_urls", generate_for_urls)
    generate = crawlers_api.get_generator(None)

    result = await generate(LIST_URL, DETAIL_URL, "")

    assert modes == [STATIC, PLAYWRIGHT]
    assert result.render_mode == STATIC
    assert any("렌더로 다시 만들지도 못했다" in note for note in result.notes)


def test_등록이_돌려주는_셀렉터는_그대로다() -> None:
    """올라간 경로가 무엇이든 저장되는 것은 생성이 낸 셀렉터다."""
    assert isinstance(result_for(RENDERED).selectors, SelectorSet)


class LinklessReport(Report):
    """항목은 잡았지만 링크를 못 읽은 결과."""

    def __init__(self) -> None:
        super().__init__(list_missing=False)
        self.failed_list_fields = ["list.link"]


@pytest.mark.asyncio
async def test_정적에서_링크를_못_읽으면_렌더로도_만들어_나은_쪽을_쓴다(
    monkeypatch: pytest.MonkeyPatch, modes: list[str]
) -> None:
    """CJ 실측(2026-09-17): 껍데기에서 항목 1건·링크 0건을 잡고 렌더로 올리지 않았다."""

    async def generate_for_urls(
        list_url: str, detail_url: str, *, source: Any, **kwargs: Any
    ) -> GenerationResult:
        html = (await source.fetch(list_url)).text
        result = result_for(RENDERED)
        if html == SHELL:
            return GenerationResult(
                selectors=result.selectors,
                usage=result.usage,
                attempts=1,
                verification=LinklessReport(),  # type: ignore[arg-type]
            )
        return result

    monkeypatch.setattr(crawlers_api, "generate_for_urls", generate_for_urls)
    generate = crawlers_api.get_generator(None)

    result = await generate(LIST_URL, DETAIL_URL, "")

    assert modes == [STATIC, PLAYWRIGHT]
    assert result.render_mode == PLAYWRIGHT
    assert any("렌더한 HTML 로 셀렉터를 만들었다" in note for note in result.notes)


@pytest.mark.asyncio
async def test_렌더해도_링크를_못_읽으면_정적_결과를_둔다(
    monkeypatch: pytest.MonkeyPatch, modes: list[str]
) -> None:
    """링크를 목록에 두지 않는 사이트(삼성)는 렌더해도 같다. 브라우저 모드로 바꾸지 않는다."""

    async def generate_for_urls(
        list_url: str, detail_url: str, *, source: Any, **kwargs: Any
    ) -> GenerationResult:
        result = result_for(RENDERED)
        return GenerationResult(
            selectors=result.selectors,
            usage=result.usage,
            attempts=1,
            verification=LinklessReport(),  # type: ignore[arg-type]
        )

    monkeypatch.setattr(crawlers_api, "generate_for_urls", generate_for_urls)
    generate = crawlers_api.get_generator(None)

    result = await generate(LIST_URL, DETAIL_URL, "")

    assert modes == [STATIC, PLAYWRIGHT]
    assert result.render_mode == STATIC
    assert "렌더로도 만들어 봤지만 나아지지 않았다" in result.notes[-1]


class OneItemReport(Report):
    """정적 껍데기에서 항목 모양 한 건만 잡은 결과. 링크는 읽었다."""

    items = 1


@pytest.mark.asyncio
async def test_정적_항목이_한_건뿐이면_렌더로도_만들어_더_많이_잡히는_쪽을_쓴다(
    monkeypatch: pytest.MonkeyPatch, modes: list[str]
) -> None:
    """CJ 실측(2026-09-17): 껍데기의 항목 모양 한 건으로 만든 셀렉터가 렌더 페이지에서 0건이었다."""

    async def generate_for_urls(
        list_url: str, detail_url: str, *, source: Any, **kwargs: Any
    ) -> GenerationResult:
        html = (await source.fetch(list_url)).text
        result = result_for(RENDERED)
        report = OneItemReport(list_missing=False) if html == SHELL else Report(list_missing=False)
        return GenerationResult(
            selectors=result.selectors,
            usage=result.usage,
            attempts=1,
            verification=report,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(crawlers_api, "generate_for_urls", generate_for_urls)
    generate = crawlers_api.get_generator(None)

    result = await generate(LIST_URL, DETAIL_URL, "")

    assert modes == [STATIC, PLAYWRIGHT]
    assert result.render_mode == PLAYWRIGHT


@pytest.mark.asyncio
async def test_렌더_목록이_더_많으면_링크를_못_읽어도_렌더를_쓴다(
    monkeypatch: pytest.MonkeyPatch, modes: list[str]
) -> None:
    """CJ 실측(2026-09-17): 렌더한 10건의 링크가 `javascript:` 였다. 링크는 판정이 눌러서 찾는다."""

    class TenLinklessReport(LinklessReport):
        items = 10

    async def generate_for_urls(
        list_url: str, detail_url: str, *, source: Any, **kwargs: Any
    ) -> GenerationResult:
        html = (await source.fetch(list_url)).text
        result = result_for(RENDERED)
        report = OneItemReport(list_missing=False) if html == SHELL else TenLinklessReport()
        return GenerationResult(
            selectors=result.selectors,
            usage=result.usage,
            attempts=1,
            verification=report,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(crawlers_api, "generate_for_urls", generate_for_urls)
    generate = crawlers_api.get_generator(None)

    result = await generate(LIST_URL, DETAIL_URL, "")

    assert result.render_mode == PLAYWRIGHT
