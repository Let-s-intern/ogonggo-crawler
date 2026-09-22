"""셀렉터 생성 테스트.

Gemini 를 실제로 부르지 않는다. 응답은 전부 가짜 클라이언트가 돌려주고, 확인하는 것은
재시도 정책과 로그에 남는 숫자다. 실제 호출로 하는 확인은 task 파일의 2.3.V 가 따로 한다.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from app.config import Settings
from app.selector.generator import (
    SelectorGenerationError,
    build_client,
    generate_from_html,
)

LIST_HTML = """
<html><body><ol class="jobs">
  <li><a class="t" href="/jobs/1">공고 하나</a><time>2026-08-01</time></li>
  <li><a class="t" href="/jobs/2">공고 둘</a><time>2026-08-02</time></li>
  <li><a class="t" href="/jobs/3">공고 셋</a><time>2026-08-03</time></li>
</ol></body></html>
"""

DETAIL_HTML = """
<html><body><article><h1 class="title">공고 하나</h1>
<div class="body">본문</div></article></body></html>
"""

VALID_RESPONSE = json.dumps(
    {
        "list": {"item": "ol.jobs > li", "title": "a.t", "link": "a.t", "date": "time"},
        "detail": {
            "title": "h1.title",
            "body": "div.body",
            "qualifications": "",
            "recruitment_end_at": "",
            "department": "",
        },
    }
)


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text
        self.usage_metadata = type(
            "Usage",
            (),
            {
                "prompt_token_count": 4321,
                "candidates_token_count": 120,
                "total_token_count": 4441,
            },
        )()
        self.candidates = [type("Candidate", (), {"finish_reason": "STOP"})()]


class FakeModels:
    def __init__(self, texts: list[str]) -> None:
        self._texts = texts
        self.calls: list[dict[str, Any]] = []

    async def generate_content(self, *, model: str, contents: str, config: Any) -> FakeResponse:
        self.calls.append({"model": model, "contents": contents, "config": config})
        return FakeResponse(self._texts[min(len(self.calls) - 1, len(self._texts) - 1)])


class FakeClient:
    """`client.aio.models.generate_content` 만 흉내낸다."""

    def __init__(self, *texts: str) -> None:
        self.models = FakeModels(list(texts))
        self.aio = self

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.models.calls


def settings_with_key() -> Settings:
    return Settings(gemini_api_key="테스트키", gemini_model="gemini-3.5-flash")


async def test_valid_response_becomes_selectors() -> None:
    client = FakeClient(VALID_RESPONSE)

    result = await generate_from_html(
        LIST_HTML, DETAIL_HTML, settings=settings_with_key(), client=client
    )

    assert result.selectors.list.item == "ol.jobs > li"
    assert result.attempts == 1
    assert len(client.calls) == 1


async def test_prompt_carries_cleaned_html_not_the_raw_page() -> None:
    client = FakeClient(VALID_RESPONSE)
    noisy = LIST_HTML.replace("<ol", "<script>steal()</script><ol")

    await generate_from_html(noisy, DETAIL_HTML, settings=settings_with_key(), client=client)

    prompt = client.calls[0]["contents"]
    assert "steal()" not in prompt
    assert 'ol class="jobs"' in prompt


async def test_response_schema_is_forced() -> None:
    client = FakeClient(VALID_RESPONSE)

    await generate_from_html(LIST_HTML, DETAIL_HTML, settings=settings_with_key(), client=client)

    config = client.calls[0]["config"]
    assert config["response_mime_type"] == "application/json"
    assert config["response_schema"].__name__ == "SelectorSet"


async def test_malformed_response_is_retried_once() -> None:
    client = FakeClient("여기 있습니다: {list:", VALID_RESPONSE)

    result = await generate_from_html(
        LIST_HTML, DETAIL_HTML, settings=settings_with_key(), client=client
    )

    assert result.attempts == 2
    assert len(client.calls) == 2


async def test_malformed_twice_fails_to_the_operator() -> None:
    client = FakeClient("깨진 응답", "또 깨진 응답")

    with pytest.raises(SelectorGenerationError) as caught:
        await generate_from_html(
            LIST_HTML, DETAIL_HTML, settings=settings_with_key(), client=client
        )

    assert caught.value.reason == "unparsable"
    assert len(client.calls) == 2


async def test_unknown_field_is_asked_once_more_then_refused() -> None:
    """스키마에 없는 이름은 뜻을 추측해 살리지 않는다. 한 번 더 묻고, 또 오면 거절한다 (2026-09-17).

    DeepSeek 는 같은 프롬프트에도 답 모양이 흔들려, 한 번의 어긋남으로 등록을 통째로 버리면 등록이
    거의 되지 않았다.
    """
    payload = json.loads(VALID_RESPONSE)
    payload["list"]["links"] = "a"
    client = FakeClient(json.dumps(payload))

    with pytest.raises(SelectorGenerationError) as caught:
        await generate_from_html(
            LIST_HTML, DETAIL_HTML, settings=settings_with_key(), client=client
        )

    assert caught.value.reason == "unknown_field"
    assert len(client.calls) == 2


async def test_list_field_put_under_detail_is_moved_back() -> None:
    """네이버·KT 실측: `link_template` 을 `detail` 에 넣었다. 있는 칸이라 자리만 옮긴다."""
    payload = json.loads(VALID_RESPONSE)
    payload["list"].pop("link_template", None)
    payload["detail"]["link_template"] = "https://example.com/jobs/{id}"
    client = FakeClient(json.dumps(payload))

    result = await generate_from_html(
        LIST_HTML, DETAIL_HTML, settings=settings_with_key(), client=client
    )

    assert result.selectors.list.link_template == "https://example.com/jobs/{id}"
    assert len(client.calls) == 1
    assert any(
        "`detail` 에 넣은 `link_template` 을 `list` 으로 옮겼다" in note for note in result.notes
    )


def test_misplaced_value_does_not_overwrite_the_right_place() -> None:
    from app.selector.schema import relocate_misplaced

    data, notes = relocate_misplaced(
        {"list": {"link_template": "keep/{id}"}, "detail": {"link_template": "drop/{id}"}}
    )

    assert data["list"]["link_template"] == "keep/{id}"
    assert "link_template" not in data["detail"]
    assert notes == ["`detail.link_template` 을 버렸다 — `list.link_template` 에 이미 값이 있다"]


async def test_usage_is_logged_with_model_tokens_and_latency(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = FakeClient(VALID_RESPONSE)

    with caplog.at_level(logging.INFO, logger="app.llm.gemini"):
        result = await generate_from_html(
            LIST_HTML, DETAIL_HTML, settings=settings_with_key(), client=client
        )

    assert result.usage.model == "gemini-3.5-flash"
    assert result.usage.input_tokens == 4321
    assert result.usage.output_tokens == 120
    assert result.usage.latency_ms >= 0
    logged = caplog.text
    assert "model=gemini-3.5-flash" in logged
    assert "input_tokens=4321" in logged
    assert "output_tokens=120" in logged
    assert "latency_ms=" in logged


async def test_narrowed_input_is_reported() -> None:
    client = FakeClient(VALID_RESPONSE)
    long_list = LIST_HTML.replace("</ol>", "<li>" + ("가" * 60_000) + "</li></ol>")

    result = await generate_from_html(
        long_list, DETAIL_HTML, settings=settings_with_key(), client=client
    )

    assert any("좁혔다" in note or "잘랐다" in note for note in result.notes)


def test_missing_api_key_fails_with_its_own_reason() -> None:
    with pytest.raises(SelectorGenerationError) as caught:
        build_client(Settings(gemini_api_key=""))

    assert caught.value.reason == "no_api_key"


async def test_api_key_never_reaches_the_prompt_or_log(caplog: pytest.LogCaptureFixture) -> None:
    client = FakeClient(VALID_RESPONSE)

    with caplog.at_level(logging.DEBUG, logger="app.selector.generator"):
        await generate_from_html(
            LIST_HTML, DETAIL_HTML, settings=settings_with_key(), client=client
        )

    assert "테스트키" not in client.calls[0]["contents"]
    assert "테스트키" not in caplog.text


async def test_missing_keys_become_empty_fields_instead_of_crashing() -> None:
    """롯데ON 실측: 상세 칸 몇 개를 키째 빼고 두 번 답했다. 빈 칸으로 받아 운영자가 채우게 한다."""
    payload = json.loads(VALID_RESPONSE)
    for name in ("qualifications", "recruitment_end_at", "department", "body"):
        payload["detail"].pop(name, None)
    client = FakeClient(json.dumps(payload))

    result = await generate_from_html(
        LIST_HTML, DETAIL_HTML, settings=settings_with_key(), client=client
    )

    assert result.selectors.detail.body == ""
    assert any("detail.body" in note for note in result.notes)


def _response_with(**list_fields: str) -> str:
    payload = json.loads(VALID_RESPONSE)
    payload["list"].update(list_fields)
    return json.dumps(payload)


async def test_zero_match_is_asked_again_with_what_went_wrong() -> None:
    """이노션 실측(2026-09-22): DeepSeek 가 클래스 앞의 `.` 을 빼 제목이 0개였다. 틀린 칸을 적어
    한 번 더 묻고, 맞은 답을 쓴다."""
    client = FakeClient(_response_with(title="t"), VALID_RESPONSE)

    result = await generate_from_html(
        LIST_HTML, DETAIL_HTML, settings=settings_with_key(), client=client
    )

    assert result.selectors.list.title == "a.t"
    assert result.verification.ok
    assert len(client.calls) == 2
    retry_prompt = client.calls[1]["contents"]
    assert "list.title: `t`" in retry_prompt
    assert "클래스는 `.이름`" in retry_prompt
    assert result.usage.input_tokens == 4321 * 2


async def test_zero_match_is_asked_only_once_more_and_keeps_the_better_answer() -> None:
    worse = _response_with(item="ul.none", title="b.none")
    better = _response_with(title="t")
    client = FakeClient(better, worse)

    result = await generate_from_html(
        LIST_HTML, DETAIL_HTML, settings=settings_with_key(), client=client
    )

    assert len(client.calls) == 2
    assert result.selectors.list.item == "ol.jobs > li"
    assert result.verification.failed_list_fields == ["list.title"]


async def test_empty_field_retry_says_why_it_was_refused() -> None:
    """HD현대 실측: 목록을 비운 답이 같은 질문에 두 번 왔다. 두 번째는 이유를 적어 묻는다."""
    client = FakeClient(_response_with(item=""), VALID_RESPONSE)

    result = await generate_from_html(
        LIST_HTML, DETAIL_HTML, settings=settings_with_key(), client=client
    )

    assert result.verification.ok
    assert "[직전 답이 거절됐다]" in client.calls[1]["contents"]
    assert "반복 요소를 찾아" in client.calls[1]["contents"]


TABLE_LIST_HTML = """
<html><body><table class="board"><tbody>
  <tr><td class="t"><a href="/v/1">공고 하나</a></td><td class="d">2026-09-14 ~ 2026-09-27</td>
      <td class="hit">322</td></tr>
  <tr><td class="t"><a href="/v/2">공고 둘</a></td><td class="d">2026-07-30 ~ 2026-08-06</td>
      <td class="hit">186</td></tr>
</tbody></table></body></html>
"""


def _table_response(date: str) -> str:
    payload = json.loads(VALID_RESPONSE)
    payload["list"] = {"item": "table.board tr", "title": "td.t a", "link": "td.t a", "date": date}
    return json.dumps(payload)


async def test_날짜_칸이_숫자뿐이면_다시_묻는다() -> None:
    """카페스 실측(2026-09-22): 날짜로 조회수 칸을 골랐다."""
    client = FakeClient(_table_response("td.hit"), _table_response("td.d"))

    result = await generate_from_html(
        TABLE_LIST_HTML, DETAIL_HTML, settings=settings_with_key(), client=client
    )

    assert result.selectors.list.date == "td.d"
    assert "날짜가 아니라 숫자뿐이다(예: 322)" in client.calls[1]["contents"]


async def test_다시_물어도_날짜_칸이_숫자뿐이면_비운다() -> None:
    client = FakeClient(_table_response("td.hit"))

    result = await generate_from_html(
        TABLE_LIST_HTML, DETAIL_HTML, settings=settings_with_key(), client=client
    )

    assert len(client.calls) == 2
    assert result.selectors.list.date == ""
    assert any("숫자(322)를 잡아 비웠다" in note for note in result.notes)
