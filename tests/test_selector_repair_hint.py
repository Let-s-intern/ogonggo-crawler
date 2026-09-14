"""운영자 힌트를 실은 AI 수정 (20.1).

2026-08-23 LG(크롤러 16)의 `list.link` 를 픽스처로 재현한다. `tests/fixtures/lg-list-20260824.html`
은 그날 렌더된 실제 목록에서 항목 3개만 남긴 것이다. 항목 안에 `a` 태그가 0개고 상세 id 가
어느 속성에도 없어서, 모델이 HTML 만 보고는 고를 것이 없었다.

사람은 브라우저에서 그 자리를 볼 수 있다. 그것을 글로 실어 보내는 통로가 힌트고, 여기서
확인하는 것은 넷이다.

- 힌트가 프롬프트에 실리는가. 셀렉터 경로든 설명 문장이든 그대로 간다
- 힌트가 없으면 프롬프트가 힌트 이전과 같은가. 안 준 실행이 준 실행처럼 굴면 안 된다
- 상한을 넘는 힌트가 잘리고, 잘린 사실이 결과에 남는가
- **힌트를 받아도 검증을 거치는가.** 고친 셀렉터는 힌트가 있든 없든 같은 HTML 에 다시 돌린다

Gemini 를 부르지 않는다. 응답은 전부 가짜 클라이언트가 돌려준다 — 무료 한도가 분당 20회라
검증에서 실호출을 반복하지 않는다.
"""

from __future__ import annotations

import json
import logging
import pathlib
from typing import Any

import pytest

from app.config import Settings
from app.selector.repair import (
    MAX_HINT_CHARS,
    MAX_PROMPT_CHARS,
    SelectorRepairError,
    normalize_hint,
    repair_from_html,
)
from app.selector.schema import validate_selectors
from tests.test_selector_generator import FakeClient
from tests.test_selector_repair import DETAIL_HTML as WORKING_DETAIL
from tests.test_selector_repair import LIST_HTML as WORKING_LIST
from tests.test_selector_repair import response as _working_response

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
LG_LIST = (FIXTURES / "lg-list-20260824.html").read_text(encoding="utf-8")
LG_URL = "https://careers.lg.com/apply"

# 운영자가 F12 의 `Copy selector` 로 딴 경로 그대로. 자동 생성 클래스와 nth-child 가 둘 다
# 들어 있다 — 이대로 저장하면 다음 배포에 깨진다
HINT_PATH = (
    "#root > div > div > main > div > div.MuiBox-root.css-jj9lbc > div > div > "
    "div.MuiBox-root.css-1jelp97 > div:nth-child(2) > div"
)

# 크롤러 16 에 저장돼 있던 모양. `list.link` 만 실패다
LG: dict[str, Any] = {
    "list": {
        "item": "div.css-13xukit",
        "title": "p.css-1pvxq8e",
        "link": "",
        "date": "div.css-5q0q11 > p:nth-of-type(1)",
        "company_name": "p.css-1swfevn",
        "link_template": "",
    },
    "detail": {
        "title": "p.css-bhz39n",
        "body": "div.css-fk1prp",
        "qualifications": "",
        "recruitment_end_at": "div.css-ouk44v",
        "department": "",
        "company_name": "p.css-1vxtqzb",
    },
}


def answer(**list_fields: str) -> str:
    """모델 응답 한 벌. 목록 필드만 바꿔 준다."""
    payload = json.loads(json.dumps(LG))
    payload["list"].update(list_fields)
    return json.dumps(payload, ensure_ascii=False)


# 실패한 필드가 하나도 없는 셀렉터 한 벌. 롯데 픽스처를 제대로 잡는 값이다
WORKING: dict[str, Any] = json.loads(_working_response())


def working_answer(**overrides: Any) -> str:
    """실패가 없는 셀렉터에서 몇 개만 바꾼 모델 응답."""
    payload = json.loads(json.dumps(WORKING))
    payload["list"].update(overrides.pop("list", {}))
    payload["detail"].update(overrides.pop("detail", {}))
    payload["list"].update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def settings_with_key() -> Settings:
    return Settings(gemini_api_key="테스트키", gemini_model="gemini-3.5-flash")


async def repair(
    *texts: str,
    hint: str = "",
    selectors: dict[str, Any] | None = None,
) -> tuple[Any, FakeClient]:
    client = FakeClient(*(texts or (answer(),)))
    outcome = await repair_from_html(
        LG_LIST,
        "",
        validate_selectors(selectors or LG),
        list_url=LG_URL,
        settings=settings_with_key(),
        client=client,
        hint=hint,
    )
    return outcome, client


def prompt_of(client: FakeClient) -> str:
    return str(client.calls[0]["contents"])


# 힌트가 프롬프트에 실린다 ---------------------------------------------------


async def test_the_hint_reaches_the_prompt() -> None:
    _, client = await repair(hint=HINT_PATH)

    prompt = prompt_of(client)
    assert "[운영자가 준 단서]" in prompt
    assert HINT_PATH in prompt


async def test_a_sentence_hint_reaches_the_prompt_too() -> None:
    """힌트는 자유 입력이다. 셀렉터가 아닌 문장도 그냥 사람이 준 단서로 싣는다."""
    hint = "마감일은 목록 항목의 두 번째 줄, 회사명 바로 아래에 있다"

    _, client = await repair(hint=hint)

    assert hint in prompt_of(client)


async def test_the_prompt_tells_the_model_not_to_copy_the_path() -> None:
    """받은 경로를 그대로 저장하면 다음 배포에 깨질 셀렉터를 심는 것이다."""
    _, client = await repair(hint=HINT_PATH)

    prompt = prompt_of(client)
    assert "그대로 베껴 쓰지 않는다" in prompt
    # 무엇이 왜 위험한지까지 적는다. "쓰지 마라" 만으로는 대안이 없다
    assert "자동 생성 클래스" in prompt
    assert ":nth-child(2)" in prompt
    assert "data-" in prompt


async def test_without_a_hint_the_prompt_is_what_it_was() -> None:
    """힌트를 안 준 실행이 준 실행과 다른 답을 내면 안 된다. 블록 자체가 붙지 않는다."""
    _, client = await repair()

    prompt = prompt_of(client)
    assert "[운영자가 준 단서]" not in prompt
    assert "그대로 베껴 쓰지 않는다" not in prompt


async def test_a_blank_hint_is_the_same_as_no_hint() -> None:
    _, without = await repair()
    _, blank = await repair(hint="   \n  ")

    assert prompt_of(blank) == prompt_of(without)


# 상한 -----------------------------------------------------------------------


def test_a_hint_within_the_cap_is_untouched() -> None:
    text, notes = normalize_hint(f"  {HINT_PATH}  ")

    assert text == HINT_PATH
    assert notes == [f"운영자 힌트 {len(HINT_PATH)}자를 함께 보냈다"]


def test_a_hint_over_the_cap_is_cut() -> None:
    """페이지를 통째로 붙여 넣는 일이 생긴다. 정제해 줄여 둔 HTML 옆에서 힌트가 입력의
    대부분을 차지하면 안 된다 (`.claude/rules/llm.md`)."""
    text, notes = normalize_hint("가" * 5_000)

    assert len(text) == MAX_HINT_CHARS
    assert notes and "상한" in notes[0] and str(MAX_HINT_CHARS) in notes[0]


async def test_a_cut_hint_says_so_in_the_result() -> None:
    """조용히 자르면 운영자는 자기가 준 단서가 다 갔다고 여긴다."""
    outcome, client = await repair(hint=HINT_PATH + " 뒤에 붙는 잡음 " + "가" * 5_000)

    assert any("상한" in note for note in outcome.notes)
    prompt = prompt_of(client)
    # 앞부분은 남는다. `Copy selector` 경로도 설명 문장도 앞이 본론이다
    assert HINT_PATH in prompt


async def test_the_whole_prompt_stays_under_the_cap() -> None:
    _, client = await repair(hint="나" * 100_000)

    assert len(prompt_of(client)) <= MAX_PROMPT_CHARS


# 힌트를 받아도 검증은 그대로 -------------------------------------------------


async def test_a_hinted_answer_is_still_run_against_the_html() -> None:
    """힌트를 받았다고 검증을 건너뛰지 않는다. 잡히지 않는 셀렉터는 실패 그대로다."""
    outcome, _ = await repair(
        answer(link="a.does-not-exist-on-this-page"),
        hint=HINT_PATH,
    )

    assert outcome.after.summary()["list.link"] == 0
    assert outcome.unresolved == ["list.link"]
    assert not outcome.ok


async def test_a_hinted_answer_that_works_is_counted_on_the_same_html() -> None:
    """전과 후가 같은 HTML 에서 나온 숫자여야 차이가 셀렉터 때문이라고 말할 수 있다.

    사이트가 다시 배포돼 `css-13xukit` 이 바뀐 상황이다. 힌트가 가리킨 자리에서 지금 쓰이는
    항목 클래스를 찾아 오면 항목과 그 안의 필드가 함께 살아난다.
    """
    stale = json.loads(json.dumps(LG))
    stale["list"]["item"] = "div.css-oldbuild"

    outcome, _ = await repair(answer(), hint=HINT_PATH, selectors=stale)

    assert outcome.before.summary()["list.item"] == 0
    assert outcome.after.summary()["list.item"] == 3
    assert outcome.after.summary()["list.title"] == 3
    assert "list.item" in outcome.repaired


async def test_without_a_hint_only_the_failed_field_can_change() -> None:
    """힌트가 없으면 대상은 실패한 필드뿐이다. 맞던 셀렉터를 두 번째 호출에서 잃지 않는다."""
    outcome, _ = await repair(answer(item="div.MuiBox-root", title="p"))

    assert outcome.targets == ["list.link"]
    assert outcome.changes == []
    assert outcome.selectors.list.item == LG["list"]["item"]
    assert outcome.selectors.list.title == LG["list"]["title"]


async def test_a_hint_widens_the_targets_beyond_the_failed_field() -> None:
    """운영자가 지적하는 자리는 실패한 필드가 아닐 수 있다. LG 는 `list.link` 가 늘 실패라,
    대상을 실패한 것으로만 두면 다른 필드는 영영 힌트로 고칠 수 없다."""
    outcome, _ = await repair(answer(title="p.css-1swfevn"), hint="제목이 회사명을 물고 온다")

    assert "list.title" in outcome.targets
    assert [change.name for change in outcome.changes] == ["list.title"]
    assert outcome.failed_targets == ["list.link"]


async def test_a_widened_target_the_model_left_alone_stays_put() -> None:
    """넓혔다고 다 바뀌는 것이 아니다. 같은 값이 오면 변경으로 세지 않는다."""
    outcome, _ = await repair(answer(), hint="제목이 회사명을 물고 온다")

    assert outcome.changes == []
    assert outcome.selectors.list.title == LG["list"]["title"]


async def test_an_empty_answer_leaves_the_field_failed() -> None:
    """LG 목록에는 상세로 가는 a 태그도 id 도 없다. 힌트가 가리킨 자리에도 없다.

    그때 모델이 할 수 있는 정답은 비워 두는 것이고, 그러면 `list.link` 는 실패로 남는다.
    억지로 아무 요소나 고르면 조용히 틀린 URL 이 공고마다 붙는다.
    """
    assert "href=" not in LG_LIST

    outcome, _ = await repair(answer(link=""), hint=HINT_PATH)

    assert outcome.changes == []
    assert outcome.unresolved == ["list.link"]
    assert outcome.selectors.list.link == ""


# 받은 경로밖에 없을 때 -------------------------------------------------------


async def test_a_position_based_answer_is_flagged_in_the_result() -> None:
    """그 자리에 안정적인 것이 없어 결국 위치로 잡았으면, 왜 그것밖에 없었는지를 남긴다."""
    outcome, _ = await repair(
        answer(link="div.MuiBox-root.css-1jelp97 > div:nth-child(2) > div"),
        hint=HINT_PATH,
    )

    flagged = [note for note in outcome.notes if "list.link" in note]
    assert flagged, outcome.notes
    assert "위치 선택자" in flagged[0]
    assert "자동 생성 클래스" in flagged[0]
    assert "다시 배포하면 깨질 수 있으니" in flagged[0]


async def test_a_stable_answer_is_not_flagged() -> None:
    outcome, _ = await repair(answer(link="p[data-role=job-link]"), hint=HINT_PATH)

    assert not [note for note in outcome.notes if "깨질 수 있으니" in note]


async def test_the_hint_length_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """무엇이 실려 나갔는지는 로그로 답한다 (`.claude/rules/llm.md`)."""
    with caplog.at_level(logging.INFO, logger="app.selector.repair"):
        await repair(hint=HINT_PATH)

    assert f"힌트={len(HINT_PATH)}자" in caplog.text


# 실패가 없어도 지적한 자리는 고친다 (20.2 보정) -------------------------------


async def working(*texts: str, hint: str = "", selectors: dict[str, Any] | None = None) -> Any:
    """실패한 필드가 하나도 없는 크롤러. 롯데 픽스처의 고쳐진 셀렉터를 쓴다."""
    client = FakeClient(*texts)
    outcome = await repair_from_html(
        WORKING_LIST,
        WORKING_DETAIL,
        validate_selectors(selectors or WORKING),
        settings=settings_with_key(),
        client=client,
        hint=hint,
    )
    return outcome, client


async def test_no_failure_and_no_hint_is_still_refused() -> None:
    """고칠 것이 없으면 부르지 않는다. 대신 무엇을 하면 되는지 사유에 적는다."""
    client = FakeClient(working_answer())

    with pytest.raises(SelectorRepairError) as caught:
        await repair_from_html(
            WORKING_LIST,
            WORKING_DETAIL,
            validate_selectors(WORKING),
            settings=settings_with_key(),
            client=client,
        )

    assert caught.value.reason == "nothing_to_repair"
    assert "힌트에 적으면" in str(caught.value)
    assert client.calls == []


async def test_a_hint_lets_the_operator_fix_a_field_that_is_not_failing() -> None:
    """실행이 성공이어도 잡히는 값이 틀릴 수 있다. 그때 고칠 길이 있어야 한다."""
    outcome, client = await working(
        working_answer(date="li.job-card > span.regdate"),
        hint="등록일이 카드 바깥의 다른 날짜를 물고 온다. 카드 안의 것만 잡아라",
    )

    assert outcome.hinted_only
    assert outcome.failed_targets == []
    assert [change.name for change in outcome.changes] == ["list.date"]
    assert outcome.selectors.list.date == "li.job-card > span.regdate"
    assert outcome.after.summary()["list.date"] == 3
    assert len(client.calls) == 1


async def test_the_hinted_prompt_does_not_call_working_fields_broken() -> None:
    _, client = await working(working_answer(), hint="날짜가 틀렸다")

    prompt = prompt_of(client)
    assert "아무것도 잡지 못한다" not in prompt
    assert "운영자가 준 단서를 읽고" in prompt
    assert "건을 잡고 있다" in prompt


async def test_fields_the_hint_did_not_mention_stay_as_they_were() -> None:
    outcome, _ = await working(
        working_answer(date="li.job-card > span.regdate"), hint="등록일이 틀렸다"
    )

    assert outcome.selectors.list.title == WORKING["list"]["title"]
    assert outcome.selectors.list.item == WORKING["list"]["item"]
    assert outcome.repaired == ["list.date"]


async def test_a_hinted_change_that_breaks_the_field_is_reported_as_unresolved() -> None:
    """힌트를 받아도 검증은 그대로다. 바꿔 놓고 안 잡히면 실패로 적는다."""
    outcome, _ = await working(working_answer(date="span.not-there"), hint="등록일이 틀렸다")

    assert outcome.after.summary()["list.date"] == 0
    assert outcome.unresolved == ["list.date"]
    assert outcome.repaired == []
    assert not outcome.ok


async def test_a_skipped_field_is_never_a_hinted_target() -> None:
    """사이트에 없는 항목을 억지로 만들면 잘못된 값이 공고마다 붙는다."""
    outcome, _ = await working(
        working_answer(detail={"qualifications": "div.made-up"}),
        hint="자격요건도 좀 채워 줘",
    )

    assert "detail.qualifications" not in outcome.targets
    assert outcome.selectors.detail.qualifications == ""
