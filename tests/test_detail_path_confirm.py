"""알아낸 상세 경로를 `httpx` 로 다시 불러 확인하는 테스트.

실사이트에 나가지 않는다. `httpx.MockTransport` 를 끼운 진짜 `Fetcher` 가 2026-08-25 픽스처를
돌려주고, 브라우저가 받은 것과 같으면 채택, 다르면 거절인지 본다.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import httpx
import pytest
from bs4 import BeautifulSoup
from bs4.element import Tag

from app.config import Settings
from app.crawler.fetcher import Fetcher
from app.crawler.playwright import ObservedRequest
from app.selector.detail_path import (
    FROM_LINK,
    DetailPath,
    IdSource,
    confirm_api_path,
    confirm_document_path,
    id_candidates,
    pick_detail_request,
    propose_detail_config,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
ROBOTS = "User-agent: *\nAllow: /\n"

SAMSUNG_DETAIL_URL = "https://www.samsungcareers.com/recruit/detail.data?seqno=22878&strCode="
SAMSUNG_BODY = (FIXTURES / "samsung-detail-20260825.json").read_text(encoding="utf-8")
LOTTE_DETAIL_URL = "https://recruit.lotte.co.kr/apply/announcement/detail/21931885"
LOTTE_HTML = (FIXTURES / "lotte-detail-20260825.html").read_text(encoding="utf-8")

SAMSUNG_ITEM = """
<li class="list">
  <a href="/#none" data-value="22,878"><p class="tit">2026년 상반기 채용</p></a>
</li>
"""


def settings() -> Settings:
    """딜레이 0. 픽스처를 돌리는 시험이 실제로 기다릴 이유가 없다."""
    return Settings(crawl_delay_seconds=0.0, crawl_max_retries=1)


def fetcher_for(handler: Any) -> Fetcher:
    return Fetcher(settings=settings(), transport=httpx.MockTransport(handler))


def item(html: str) -> Tag:
    node = BeautifulSoup(html, "html.parser").find("li")
    assert isinstance(node, Tag)
    return node


def samsung_request() -> ObservedRequest:
    return ObservedRequest(
        method="GET",
        url=SAMSUNG_DETAIL_URL,
        status=200,
        content_type="application/json;charset=utf-8",
        body=SAMSUNG_BODY,
    )


def samsung_proposal() -> tuple[Any, ObservedRequest]:
    request = samsung_request()
    picked = pick_detail_request([request], id_candidates(item(SAMSUNG_ITEM)))
    assert picked is not None
    path = propose_detail_config(*picked)
    assert path.ok is True
    return path, request


def detail_handler(body: str, *, status: int = 200) -> Any:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        return httpx.Response(
            status, text=body, headers={"content-type": "application/json;charset=utf-8"}
        )

    return handle


@pytest.mark.asyncio
async def test_같은_응답이_오면_채택한다() -> None:
    path, request = samsung_proposal()
    client = fetcher_for(detail_handler(SAMSUNG_BODY))
    try:
        confirmation = await confirm_api_path(client, path, request)
    finally:
        await client.aclose()

    assert confirmation.adopted is True
    assert confirmation.title
    assert confirmation.body_length > 0


@pytest.mark.asyncio
async def test_다른_공고가_오면_거절한다() -> None:
    """브라우저에서만 되는 요청을 저장하면 이후 실행이 전부 실패한다."""
    path, request = samsung_proposal()
    payload = json.loads(SAMSUNG_BODY)
    payload["data"]["result"]["title"] = "전혀 다른 공고"
    client = fetcher_for(detail_handler(json.dumps(payload, ensure_ascii=False)))
    try:
        confirmation = await confirm_api_path(client, path, request)
    finally:
        await client.aclose()

    assert confirmation.adopted is False
    assert "`title` 이" in confirmation.reason
    assert "헤더나 쿠키" in confirmation.reason


@pytest.mark.asyncio
async def test_로그인_페이지가_200_으로_와도_거절한다() -> None:
    path, request = samsung_proposal()
    client = fetcher_for(detail_handler("<html><body>로그인이 필요합니다</body></html>"))
    try:
        confirmation = await confirm_api_path(client, path, request)
    finally:
        await client.aclose()

    assert confirmation.adopted is False
    assert "다시 부른 응답이" in confirmation.reason


@pytest.mark.asyncio
async def test_전송이_실패하면_거절한다() -> None:
    path, request = samsung_proposal()

    def handle(http_request: httpx.Request) -> httpx.Response:
        if http_request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        return httpx.Response(403, text="forbidden")

    client = fetcher_for(handle)
    try:
        confirmation = await confirm_api_path(client, path, request)
    finally:
        await client.aclose()

    assert confirmation.adopted is False
    assert "부르지 못했다" in confirmation.reason


@pytest.mark.asyncio
async def test_공백_차이만_있으면_같은_응답으로_본다() -> None:
    """지저분한 값은 정규화가 다루는 문제이지 경로가 다르다는 뜻이 아니다."""
    path, request = samsung_proposal()
    payload = json.loads(SAMSUNG_BODY)
    payload["data"]["result"]["title"] = f"  {payload['data']['result']['title']}\n "
    client = fetcher_for(detail_handler(json.dumps(payload, ensure_ascii=False)))
    try:
        confirmation = await confirm_api_path(client, path, request)
    finally:
        await client.aclose()

    assert confirmation.adopted is True


@pytest.mark.asyncio
async def test_문서_상세는_정적_응답에_제목이_있으면_채택한다() -> None:
    """롯데. 클릭하면 같은 탭에서 상세 문서로 이동하고, 그 문서는 정적으로도 열린다."""
    title = _lotte_title()

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        return httpx.Response(200, text=LOTTE_HTML, headers={"content-type": "text/html"})

    client = fetcher_for(handle)
    try:
        confirmation = await confirm_document_path(client, LOTTE_DETAIL_URL, title)
    finally:
        await client.aclose()

    assert confirmation.adopted is True


@pytest.mark.asyncio
async def test_정적_문서에_제목이_없으면_채택하지_않는다() -> None:
    """LG 처럼 브라우저에서만 그려지는 상세다. 렌더로 둘지는 운영자가 정한다."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        return httpx.Response(200, text="<html><body><div id='root'></div></body></html>")

    client = fetcher_for(handle)
    try:
        confirmation = await confirm_document_path(
            client, "https://careers.lg.com/apply/detail?id=1002099", "보건관리자 계약직 채용"
        )
    finally:
        await client.aclose()

    assert confirmation.adopted is False
    assert "렌더로 둬야 한다" in confirmation.reason


@pytest.mark.asyncio
async def test_설정이_없으면_확인할_것이_없다고_말한다() -> None:
    client = fetcher_for(detail_handler("{}"))
    try:
        confirmation = await confirm_api_path(
            client,
            DetailPath(kind="api", id_source=IdSource(kind=FROM_LINK, detail="마지막", value="1")),
            samsung_request(),
        )
    finally:
        await client.aclose()

    assert confirmation.adopted is False
    assert "확인할 것이 없다" in confirmation.reason


def _lotte_title() -> str:
    """픽스처 문서에 실제로 있는 제목 한 줄. 문서에서 그대로 읽는다."""
    soup = BeautifulSoup(LOTTE_HTML, "html.parser")
    node = soup.find("h1") or soup.find("title")
    assert node is not None
    return node.get_text(strip=True)


@pytest.mark.asyncio
async def test_제목이_title_과_스크립트에만_있으면_채택하지_않는다() -> None:
    """recruiter.co.kr. 정적 응답은 `<title>` 과 스크립트에만 제목을 두고 본문은 비어 있다."""
    title = "HD현대마린솔루션 보건관리자 계약직 사원 모집"
    shell = (
        f"<html><head><title>{title} | HD현대 채용</title>"
        f'<meta property="og:title" content="{title}"></head>'
        f'<body><div id="__next"></div><script>{{"title": "{title}"}}</script></body></html>'
    )

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        return httpx.Response(200, text=shell, headers={"content-type": "text/html"})

    client = fetcher_for(handle)
    try:
        confirmation = await confirm_document_path(
            client, "https://hd.recruiter.co.kr/career/jobs/129233", title
        )
    finally:
        await client.aclose()

    assert confirmation.adopted is False
