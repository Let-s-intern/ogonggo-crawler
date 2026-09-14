"""폼 본문으로 물어보고 HTML 조각으로 오는 목록. 삼성이 이 경로다.

삼성 목록은 `POST /hr/list.data` 에 `application/x-www-form-urlencoded` 로 여덟 파라미터를
보내야 하고, 돌아오는 것은 JSON 이 아니라 HTML 조각이다. 파라미터가 하나라도 빠지면
`{"code":500}` 이 온다 (`.claude/site-recipes/www-samsungcareers-com.md`).

공고 번호는 `a[data-value="22,878"]` 에서 쉼표를 뺀 값이다. 숫자 표기에 기대는 자리라서
여기서 그 사실을 못으로 박아 둔다.
"""

from __future__ import annotations

import pathlib

import httpx
import pytest

from app.config import Settings
from app.crawler.api_source import build_html_items, fetch_list
from app.crawler.fetcher import Fetcher
from app.crawler.parser import FieldParseError, SelectorMissError
from app.selector.api_schema import ApiConfigError, validate_api_config

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
PAGE_ONE = (FIXTURES / "samsung-list-p1-20260825.html").read_text(encoding="utf-8")

ROBOTS = "User-agent: *\nAllow: /\n"

FORM = {
    "currentPageNo": 1,
    "intNo": 0,
    "strVal": "",
    "strTxt": "",
    "strKey": "",
    "strCompany": "",
    "strType": "",
    "strOrderBy": "",
    "strEntity": "",
}

LIST_CONFIG = validate_api_config(
    {
        "list": {
            "url": "https://www.samsungcareers.com/hr/list.data",
            "method": "POST",
            "body": FORM,
            "body_format": "form",
            "response": "html",
            "headers": {"referer": "https://www.samsungcareers.com/hr/"},
            "items_path": "li",
            "fields": {
                "title": "h3.title",
                "date": "span.period",
                "company_name": "p.company",
            },
            "id_field": "a[data-value]@data-value|digits",
            "link_template": "https://www.samsungcareers.com/recruit/detail.data?seqno={id}",
        }
    }
).list_config()


def test_the_html_list_yields_every_item_on_the_page() -> None:
    """1쪽 9건. 항목마다 제목·회사·기간과 공고 번호가 나온다."""
    result = build_html_items(PAGE_ONE, LIST_CONFIG)

    assert result.matched == 9
    assert len(result.items) == 9
    assert result.failures == []


def test_the_posting_number_drops_the_thousands_separator() -> None:
    """`22,878` 이 그대로 주소에 들어가면 `%2C` 로 인코딩돼 열리지 않는다."""
    result = build_html_items(PAGE_ONE, LIST_CONFIG)

    first = result.items[0]
    assert first.detail_key == "22878"
    assert first.link == "https://www.samsungcareers.com/recruit/detail.data?seqno=22878"
    assert all(item.detail_key.isdigit() for item in result.items)


def test_every_item_gets_its_own_address() -> None:
    """공고마다 주소가 달라야 중복 판정과 소비 측 링크가 선다."""
    result = build_html_items(PAGE_ONE, LIST_CONFIG)

    assert len({item.link for item in result.items}) == 9


def test_an_empty_page_is_a_failure_not_an_empty_success() -> None:
    """0건은 신규 없음이 아니라 목록을 못 읽은 것이다."""
    with pytest.raises(SelectorMissError):
        build_html_items("<div>공고가 없습니다</div>", LIST_CONFIG)


def test_an_id_field_without_an_attribute_says_so() -> None:
    """HTML 모드의 `id_field` 는 `<셀렉터>@<속성>` 이다. JSON 키를 적으면 그렇게 말한다."""
    config = validate_api_config(
        {
            "list": {
                "url": "https://www.samsungcareers.com/hr/list.data",
                "response": "html",
                "items_path": "li",
                "fields": {"title": "h3.title"},
                "id_field": "seqno",
                "link_template": "https://www.samsungcareers.com/recruit/detail.data?seqno={id}",
            }
        }
    ).list_config()

    with pytest.raises(FieldParseError) as caught:
        build_html_items(PAGE_ONE, config)

    assert "id_field" in str(caught.value)


async def test_the_form_body_actually_goes_out_as_a_form() -> None:
    """JSON 으로 나가면 삼성은 500 을 준다. 실제로 폼으로 나가는지 요청을 본다."""
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=ROBOTS)
        content_type = request.headers.get("content-type", "")
        if not content_type.startswith("application/x-www-form-urlencoded"):
            return httpx.Response(200, text='{"code":500}')
        return httpx.Response(200, text=PAGE_ONE, headers={"content-type": "text/html"})

    client = Fetcher(
        settings=Settings(crawl_delay_seconds=0.0, crawl_max_retries=1),
        transport=httpx.MockTransport(handle),
    )
    result = await fetch_list(client, LIST_CONFIG)
    await client.aclose()

    sent = [request for request in seen if request.url.path.endswith("list.data")]
    assert len(sent) == 1
    body = sent[0].content.decode()
    assert "currentPageNo=1" in body
    assert "strEntity=" in body
    assert len(result.items) == 9


def test_an_unknown_body_format_is_refused() -> None:
    """오타를 조용히 JSON 으로 되돌리지 않는다."""
    with pytest.raises(ApiConfigError) as caught:
        validate_api_config(
            {
                "list": {
                    "url": "https://example.test/jobs",
                    "items_path": "data.list",
                    "fields": {"title": "name"},
                    "id_field": "id",
                    "link_template": "https://example.test/jobs/{id}",
                    "body_format": "multipart",
                }
            }
        )

    assert caught.value.reason == "unknown_field"
