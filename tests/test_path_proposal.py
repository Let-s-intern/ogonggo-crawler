"""판정이 막힌 자리에서 AI 에게 목록 경로를 묻고, 답을 확인하는 것 테스트.

모델은 부르지 않는다. 정해 둔 답을 돌려주는 함수를 끼우고, 그 답이 확인을 통과하는지만 본다.
사이트는 `httpx.MockTransport` 다. 값은 2026-09-21 HD현대·동원 실측에서 왔다.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.crawler.fetcher import Fetcher
from app.crawler.parser import ListItem
from app.crawler.playwright import ObservedRequest
from app.llm.base import LlmCallError, Usage
from app.selector.path_proposal import (
    PathAnswer,
    PathEvidence,
    ProposedHeader,
    build_prompt,
    propose_path,
)

ROBOTS = "User-agent: *\nDisallow: /*?*page=\n"
TODAY = date(2026, 9, 21)
LIST_URL = "https://careers.example.test/recruit"
PAGED = "https://careers.example.test/_backend/recruitments?companyId=c1&page=1&countPerPage=10"
WHOLE = "https://careers.example.test/_backend/recruitments?companyId=c1&countPerPage=100"
CLICKED = "https://careers.example.test/job_posting/KSO5rowc"

ENTRIES = [
    {"addressKey": "KSO5rowc", "externalTitle": "전기 견적 경력직", "deadlineValue": None},
    {
        "addressKey": "RdAkTmTg",
        "externalTitle": "기계 견적 경력직",
        "deadlineValue": "2026-10-05T14:59:59.000Z",
    },
    # 지난 차수의 같은 제목. 화면에는 새 차수만 떠 있다
    {
        "addressKey": "OLD12345",
        "externalTitle": "기계 견적 경력직",
        "deadlineValue": "2026-05-31T14:59:59.000Z",
    },
]
BODY = json.dumps({"count": 3, "results": ENTRIES}, ensure_ascii=False)
ITEMS = [
    ListItem(index=0, title="전기 견적 경력직", link="", date="", detail_absent=True),
    ListItem(index=1, title="기계 견적 경력직", link="", date="", detail_absent=True),
]
USAGE = Usage(
    provider="deepseek",
    model="deepseek-flash",
    input_tokens=2400,
    output_tokens=650,
    total_tokens=3050,
    latency_ms=2900,
)


def evidence(**overrides: Any) -> PathEvidence:
    values: dict[str, Any] = {
        "list_url": LIST_URL,
        "items": ITEMS,
        "known_links": [("전기 견적 경력직", CLICKED)],
        "requests": [
            ObservedRequest(
                method="GET",
                url=PAGED,
                status=200,
                content_type="application/json",
                body=BODY,
                request_headers={"x-tenant": "dongwon"},
            )
        ],
        "robots": ROBOTS,
    }
    values.update(overrides)
    return PathEvidence(**values)


def answer(**overrides: Any) -> PathAnswer:
    values: dict[str, Any] = {
        "found": True,
        "url": WHOLE,
        "method": "GET",
        "body_json": "",
        "items_path": "results",
        "title_field": "externalTitle",
        "date_field": "deadlineValue",
        "date_is_deadline": True,
        "company_field": "",
        "id_field": "addressKey",
        "link_template": "https://careers.example.test/job_posting/{id}",
        "headers": [],
        "reason": "page 를 빼고 한 번에 받는다",
    }
    values.update(overrides)
    return PathAnswer(**values)


def answering(reply: PathAnswer, prompts: list[str] | None = None) -> Any:
    async def ask(prompt: str) -> tuple[PathAnswer, Usage | None]:
        if prompts is not None:
            prompts.append(prompt)
        return reply, USAGE

    return ask


def site(seen: list[str] | None = None, body: str = BODY) -> Fetcher:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        if seen is not None:
            seen.append(str(request.url))
        if request.url.path == "/_backend/recruitments":
            return httpx.Response(200, text=body, headers={"content-type": "application/json"})
        return httpx.Response(404, text="not found")

    settings = Settings(crawl_delay_seconds=0.0, crawl_max_retries=1)
    return Fetcher(settings=settings, transport=httpx.MockTransport(handle))


async def run(reply: PathAnswer, **overrides: Any) -> Any:
    client = site()
    try:
        return await propose_path(answering(reply), client, evidence(**overrides), today=TODAY)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_확인을_통과한_제안은_목록_경로가_된다() -> None:
    """동원. robots 가 막은 `page=` 를 빼고 한 번에 받는 주소다."""
    proposal = await run(answer())

    assert proposal.path is not None
    config = proposal.path.config()
    assert config.url == WHOLE
    assert config.id_field == "addressKey"
    assert config.date_is_deadline is True
    assert proposal.usage == USAGE
    assert "확인한 공고 주소 1건 일치" in proposal.note


@pytest.mark.asyncio
async def test_robots_가_막은_주소를_제안하면_버린다() -> None:
    proposal = await run(answer(url=PAGED))

    assert proposal.path is None
    assert "robots" in proposal.note


@pytest.mark.asyncio
async def test_누른_공고의_주소가_다르게_나오면_버린다() -> None:
    """형식을 지어내면 여기서 떨어진다. 실제로 누른 공고의 주소가 기준이다."""
    proposal = await run(answer(link_template="https://careers.example.test/o/{id}"))

    assert proposal.path is None
    assert CLICKED in proposal.note


@pytest.mark.asyncio
async def test_관찰하지_않은_호스트는_부르지_않는다() -> None:
    seen: list[str] = []
    client = site(seen)
    try:
        proposal = await propose_path(
            answering(answer(url="https://evil.example.test/list")),
            client,
            evidence(),
            today=TODAY,
        )
    finally:
        await client.aclose()

    assert proposal.path is None
    assert "관찰한 적 없는 호스트" in proposal.note
    assert seen == []


@pytest.mark.asyncio
async def test_화면에_떠_있는_공고의_날짜가_지났으면_마감일로_보지_않는다() -> None:
    """게시일을 마감일로 읽으면 어제 올라온 공고가 조용히 버려진다."""
    posted = json.dumps(
        {
            "results": [
                {
                    "addressKey": "KSO5rowc",
                    "externalTitle": "전기 견적 경력직",
                    "createdAt": "2026-09-16",
                },
                {
                    "addressKey": "RdAkTmTg",
                    "externalTitle": "기계 견적 경력직",
                    "createdAt": "2026-09-10",
                },
            ]
        },
        ensure_ascii=False,
    )
    client = site(body=posted)
    try:
        proposal = await propose_path(
            answering(answer(date_field="createdAt")), client, evidence(), today=TODAY
        )
    finally:
        await client.aclose()

    assert proposal.path is not None
    config = proposal.path.config()
    # 칸은 남기고 마감일로만 보지 않는다
    assert config.fields["date"] == "createdAt"
    assert config.date_is_deadline is False
    assert "마감일로 보지 않는다" in proposal.note


@pytest.mark.asyncio
async def test_같은_제목의_지난_차수는_마감일_판단을_흔들지_않는다() -> None:
    """HD현대 실측. 556건 안에 5월에 끝난 같은 제목의 공고가 있었다."""
    proposal = await run(answer())

    assert proposal.path is not None
    assert proposal.path.config().date_is_deadline is True


@pytest.mark.asyncio
async def test_제안에_붙은_신원_헤더는_떨어진다() -> None:
    proposal = await run(
        answer(
            headers=[
                ProposedHeader(name="x-tenant", value="dongwon"),
                ProposedHeader(name="cookie", value="SESSION=abc"),
                ProposedHeader(name="sentry-trace", value="d69ba255-ac10-0"),
            ]
        )
    )

    assert proposal.path is not None
    assert proposal.path.config().headers == {"x-tenant": "dongwon"}


@pytest.mark.asyncio
async def test_AI_가_못_찾았다고_하면_그대로_적는다() -> None:
    proposal = await run(answer(found=False, reason="목록을 담은 요청이 없다"))

    assert proposal.path is None
    assert "목록을 담은 요청이 없다" in proposal.note
    assert proposal.usage == USAGE


@pytest.mark.asyncio
async def test_호출이_실패해도_판정은_계속된다() -> None:
    async def broken(prompt: str) -> tuple[PathAnswer, Usage | None]:
        raise LlmCallError("no_api_key", "키가 없다")

    client = site()
    try:
        proposal = await propose_path(broken, client, evidence(), today=TODAY)
    finally:
        await client.aclose()

    assert proposal.path is None
    assert "키가 없다" in proposal.note


def test_프롬프트에는_응답_골격만_들어간다() -> None:
    """HD현대 목록 응답은 1.4MB 다. 통째로 보내지 않는다."""
    huge = json.dumps(
        {
            "data": [
                {"sn": index, "name": f"공고 {index}", "contents": "<p>본문</p>" * 5000}
                for index in range(300)
            ]
        },
        ensure_ascii=False,
    )
    prompt = build_prompt(
        evidence(
            items=[
                ListItem(index=0, title="공고 1", link="", date=""),
                ListItem(index=1, title="공고 2", link="", date=""),
            ],
            known_links=[("공고 1", "https://careers.example.test/job_posting/1")],
            requests=[
                ObservedRequest(
                    method="GET",
                    url=PAGED,
                    status=200,
                    content_type="application/json",
                    body=huge,
                )
            ],
        )
    )

    assert len(huge) > 1_000_000
    assert len(prompt) < 10_000
    assert "배열 `data` (300건)" in prompt
    # 확인한 주소의 항목은 첫 항목과 따로 보인다. id 를 짚을 근거다
    assert "확인한 공고 주소의 항목:" in prompt
    assert "Disallow: /*?*page=" in prompt
