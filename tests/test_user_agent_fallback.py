"""빈 CRAWL_USER_AGENT 가 이름 없는 요청으로 새지 않는지."""

import pytest

from app.config import Settings
from app.crawler.fetcher import UNSET_USER_AGENT, Fetcher


@pytest.mark.parametrize("value", ["", "   ", "\t"])
def test_빈_값은_설정하지_않은_것으로_본다(value: str) -> None:
    # compose 는 변수를 넣지 않아도 "" 를 채워 넘긴다. 그대로 두면 이름 없이 나간다
    assert Fetcher(settings=Settings(crawl_user_agent=value)).user_agent == UNSET_USER_AGENT


def test_채운_값은_그대로_나간다() -> None:
    ua = "ogonggo-ai (contact: someone@example.com)"
    assert Fetcher(settings=Settings(crawl_user_agent=ua)).user_agent == ua
