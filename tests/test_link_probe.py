"""클릭으로 알아낸 주소를 공고마다 다른 주소 형식으로 옮기는 것 테스트.

픽스처는 2026-08-25 의 두산·네이버 목록이다. 둘 다 항목의 `href` 가 `javascript:` 라
따라갈 수 없고, 공고 번호는 `onclick` 인자에 들어 있다.

| 사이트 | 클릭하면 | 형식의 출처 |
|---|---|---|
| 네이버 | `view.do?annoId=30005276` 으로 이동한다 | 도착한 주소 |
| 두산 | 같은 주소로 폼 POST 가 나간다 | 그때 나간 요청 |

실사이트에 나가지 않는다. 확인 경로만 `httpx.MockTransport` 다.
"""

from __future__ import annotations

import pathlib
from typing import Any

import httpx
import pytest
from bs4 import BeautifulSoup

from app.config import Settings
from app.crawler.fetcher import Fetcher
from app.crawler.playwright import ObservedRequest
from app.selector.link import resolve_link
from app.selector.link_probe import (
    confirm_link_template,
    propose_link_template,
    value_sources,
)
from app.selector.schema import ListSelectors

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
ROBOTS = "User-agent: *\nAllow: /\n"

NAVER_LIST_URL = "https://recruit.navercorp.com/rcrt/list.do"
NAVER_REACHED = (
    "https://recruit.navercorp.com/rcrt/view.do"
    "?annoId=30005276&sw=&subJobCdArr=&sysCompanyCdArr=&empTypeCdArr="
)
DOOSAN_LIST_URL = "https://career.doosan.com/dsp/sa/RecList.jsp"
DOOSAN_BODY = (
    "REC_ID=1000361539&REC_TYPE_CD=C_REC_TYPE_02&q_REC_TYPE=&REC_MGT_CD=C_REC_MGT_04"
    "&OPEN_YN=&q_CHRG_ID=&q_SCHFIRM_ID=&BA_STATUS_CD=&q_COMP_CD=08000002"
    "&PRE_URL=REC&MENU_ID=RecList&mode=goDetail"
)


def nodes(fixture: str, item: str) -> list[Any]:
    html = (FIXTURES / fixture).read_text(encoding="utf-8")
    return BeautifulSoup(html, "html.parser").select(item)


def naver_nodes() -> list[Any]:
    return nodes("naver-list-20260825.html", "li.card_item")


def doosan_nodes() -> list[Any]:
    return nodes("doosan-list-20260825.html", "ul.list-cont > li")


def titles(items: list[Any], selector: str) -> list[str]:
    found = []
    for node in items:
        picked = node.select(selector)
        found.append(picked[0].get_text(strip=True) if picked else "")
    return found


def settings() -> Settings:
    return Settings(crawl_delay_seconds=0.0, crawl_max_retries=1)


def fetcher_for(handler: Any) -> Fetcher:
    return Fetcher(settings=settings(), transport=httpx.MockTransport(handler))


def test_네이버는_도착한_주소를_형식으로_옮긴다() -> None:
    items = naver_nodes()
    assert len(items) == 10

    proposal = propose_link_template(items, reached_url=NAVER_REACHED, list_url=NAVER_LIST_URL)

    assert proposal.ok is True
    assert proposal.selector == "a.card_link"
    assert proposal.template.startswith(
        "https://recruit.navercorp.com/rcrt/view.do?annoId={onclick|arg1}"
    )
    # 항목마다 실제로 주소가 나와야 한다. 첫 항목만 되는 형식은 쓸 수 없다
    assert (proposal.resolved, proposal.count) == (10, 10)


def test_두산은_폼_POST_를_GET_주소로_옮긴다() -> None:
    """주소가 그대로인 사이트다. 도착한 주소가 아니라 그때 나간 요청을 본다."""
    items = doosan_nodes()
    request = ObservedRequest(
        method="POST",
        url=DOOSAN_LIST_URL,
        status=200,
        content_type="text/html",
        request_body=DOOSAN_BODY,
        body="<html>상세</html>",
    )

    proposal = propose_link_template(
        items, reached_url=DOOSAN_LIST_URL, list_url=DOOSAN_LIST_URL, requests=[request]
    )

    assert proposal.ok is True
    assert proposal.selector == "a.list-tit"
    assert "REC_ID={onclick|arg1}" in proposal.template
    assert "REC_MGT_CD={onclick|arg2}" in proposal.template
    assert "mode=goDetail" in proposal.template
    assert (proposal.resolved, proposal.count) == (29, 29)


def test_호스트는_자리표시자로_바꾸지_않는다() -> None:
    """네이버 공유 링크의 인자에 `naver` 가 있다. 도메인 안에서 바뀌면 주소가 무너진다."""
    proposal = propose_link_template(
        naver_nodes(), reached_url=NAVER_REACHED, list_url=NAVER_LIST_URL
    )

    assert proposal.template.startswith("https://recruit.navercorp.com/")


def test_공유_버튼은_값의_출처로_쓰지_않는다() -> None:
    """공유 링크에도 공고 번호가 들어 있지만 그것으로는 상세에 가지 못한다."""
    sources = value_sources(naver_nodes()[0])

    assert [one.selector for one in sources if "social" in one.selector] == []
    assert any(one.placeholder == "{onclick|arg1}" and one.value == "30005276" for one in sources)


def test_값이_하나도_없으면_형식을_만들지_않는다() -> None:
    proposal = propose_link_template(
        naver_nodes(),
        reached_url="https://recruit.navercorp.com/rcrt/view.do?lang=ko",
        list_url=NAVER_LIST_URL,
    )

    assert proposal.ok is False
    assert "값이 하나도 없다" in proposal.reason


def test_만든_형식은_항목마다_다른_주소를_낸다() -> None:
    proposal = propose_link_template(
        naver_nodes(), reached_url=NAVER_REACHED, list_url=NAVER_LIST_URL
    )
    selectors = ListSelectors(
        item="", title="", link=proposal.selector, date="", link_template=proposal.template
    )

    urls = [resolve_link(node, selectors).url for node in naver_nodes()]

    assert len(set(urls)) == len(urls)
    assert urls[0].startswith("https://recruit.navercorp.com/rcrt/view.do?annoId=30005276")


@pytest.mark.asyncio
async def test_두_건을_열어_제목이_있으면_채택한다() -> None:
    items = naver_nodes()
    found = titles(items, "h4.card_title")
    proposal = propose_link_template(items, reached_url=NAVER_REACHED, list_url=NAVER_LIST_URL)
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        seen.append(str(request.url))
        anno = request.url.params.get("annoId", "")
        index = [str(node.select_one("a.card_link")["onclick"]) for node in items].index(
            f"show('{anno}')"
        )
        return httpx.Response(200, text=f"<html><h1>{found[index]}</h1></html>")

    client = fetcher_for(handle)
    try:
        confirmation = await confirm_link_template(client, proposal, items, found)
    finally:
        await client.aclose()

    assert confirmation.adopted is True
    assert confirmation.checked == 2
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_두_번째_항목이_같은_페이지면_채택하지_않는다() -> None:
    """첫 항목만 맞고 나머지가 전부 같은 곳을 가리키는 형식을 거른다."""
    items = naver_nodes()
    found = titles(items, "h4.card_title")
    proposal = propose_link_template(items, reached_url=NAVER_REACHED, list_url=NAVER_LIST_URL)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        # 어느 주소로 물어도 첫 공고를 돌려준다
        return httpx.Response(200, text=f"<html><h1>{found[0]}</h1></html>")

    client = fetcher_for(handle)
    try:
        confirmation = await confirm_link_template(client, proposal, items, found)
    finally:
        await client.aclose()

    assert confirmation.adopted is False
    assert "제목" in confirmation.reason


@pytest.mark.asyncio
async def test_열리지_않는_주소는_채택하지_않는다() -> None:
    items = naver_nodes()
    found = titles(items, "h4.card_title")
    proposal = propose_link_template(items, reached_url=NAVER_REACHED, list_url=NAVER_LIST_URL)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        return httpx.Response(404, text="not found")

    client = fetcher_for(handle)
    try:
        confirmation = await confirm_link_template(client, proposal, items, found)
    finally:
        await client.aclose()

    assert confirmation.adopted is False
    assert "열지 못했다" in confirmation.reason


def test_항목_자체가_a_인_사이트는_그_href_를_형식으로_쓴다() -> None:
    """카카오 목록은 `<a><li>...</li></a>` 라 항목 안에서 링크를 찾을 수 없다."""
    items = nodes("kakao-list-20260825.html", "ul.list_jobs > a")
    assert len(items) == 11

    proposal = propose_link_template(
        items,
        reached_url=(
            "https://careers.kakao.com/jobs/P-14503"
            "?skillSet=&part=BUSINESS_SERVICES&company=KAKAO&employeeType=&page=1"
        ),
        list_url="https://careers.kakao.com/jobs?part=BUSINESS_SERVICES&company=KAKAO&page=1",
    )

    assert proposal.ok is True
    # 항목 노드 자신의 속성을 읽는다는 뜻이다 (`app/selector/link.py`)
    assert proposal.selector == ""
    assert proposal.template == "https://careers.kakao.com{href}"
    assert (proposal.resolved, proposal.count) == (11, 11)


GC_ITEMS = """
<div id="listBody">
  <div class="list-item recruit"><input type="hidden" value="o" class="listType">
    <input type="hidden" value="2859" class="key"><div class="job-title">해외영업(경력)</div></div>
  <div class="list-item recruit"><input type="hidden" value="o" class="listType">
    <input type="hidden" value="2871" class="key"><div class="job-title">생산(신입)</div></div>
</div>
"""
GC_REACHED = "https://careers.gcbiopharma.com/job/GC-%ED%95%B4%EC%99%B8/4467050/"


def test_리다이렉트_전_주소와_숨은_입력값으로_형식을_만든다() -> None:
    """GC녹십자 실측(2026-09-22): 공고 번호는 숨은 입력값에만 있고, 누르면 `window.open` 으로 연
    `job-invite/2859/` 가 번호 없는 주소로 리다이렉트된다. 도착한 주소만 보면 재료가 없다."""
    items = BeautifulSoup(GC_ITEMS, "html.parser").select("div.list-item.recruit")

    proposal = propose_link_template(
        items,
        reached_url=GC_REACHED,
        list_url="https://recruit.gccorp.com/kor/recruit/list",
        navigations=["https://careers.gcbiopharma.com/job-invite/2859/", GC_REACHED],
    )

    assert proposal.ok is True
    assert proposal.template == "https://careers.gcbiopharma.com/job-invite/{value}/"
    selectors = ListSelectors(
        item="", title="", link=proposal.selector, date="", link_template=proposal.template
    )
    assert [resolve_link(node, selectors).url for node in items] == [
        "https://careers.gcbiopharma.com/job-invite/2859/",
        "https://careers.gcbiopharma.com/job-invite/2871/",
    ]
