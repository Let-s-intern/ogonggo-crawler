"""모델 호출 기록 테스트 (1.6.V).

셀렉터 생성만 있을 때는 로그 줄 하나로 충분했다. 본문 분류는 공고마다 하나씩 붙어서, 남기지
않으면 "이번 달에 얼마나 썼나" 에 답할 길이 없다 (`migrations/0013_llm_calls.sql`).

여기서 보는 것은 넷이다 — 호출 하나가 행 하나로 남는가, 실패한 호출도 남는가,
**기록이 실패해도 호출이 실패로 바뀌지 않는가**, 그리고 **어느 제공자에 돈이 나갔는지 남는가.**

마지막이 제공자가 넷이 된 뒤에 생긴 것이다. 예전에는 `app/llm/log.py` 가 gemini 라는 상수를
가져다 박고 있어서, 다른 제공자로 부른 호출도 gemini 로 적혔다.
"""

from __future__ import annotations

import pathlib
import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import db
from app.llm.base import Usage
from app.llm.log import (
    CLASSIFY,
    IMAGE_READ,
    SELECTOR_GENERATE,
    SELECTOR_REPAIR,
    by_feature,
    record_call,
    totals,
)
from tests.test_api_crawlers import (  # noqa: F401  (fixture 를 그대로 쓴다)
    DETAIL_URL,
    GENERATED,
    LIST_URL,
    USAGE,
    client,
    conn,
    result_for,
    use_generator,
)

USAGE_ONE = Usage(
    provider="gemini",
    model="gemini-3.5-flash",
    input_tokens=4321,
    output_tokens=120,
    total_tokens=4441,
    latency_ms=5100,
)
USAGE_TWO = Usage(
    provider="gemini",
    model="gemini-3.5-flash",
    input_tokens=1000,
    output_tokens=50,
    total_tokens=1050,
    latency_ms=900,
)


@pytest.fixture
def logged(tmp_path: pathlib.Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect(tmp_path / "calls.db")
    db.migrate_up(connection)
    try:
        yield connection
    finally:
        connection.close()


def test_one_call_is_one_row_with_provider_model_feature_tokens_and_latency(
    logged: sqlite3.Connection,
) -> None:
    record_call(logged, feature=CLASSIFY, usage=USAGE_ONE)

    row = logged.execute("SELECT * FROM llm_calls").fetchone()
    assert row["provider"] == "gemini"
    assert row["model"] == "gemini-3.5-flash"
    assert row["feature"] == CLASSIFY
    assert row["input_tokens"] == 4321
    assert row["output_tokens"] == 120
    assert row["total_tokens"] == 4441
    assert row["latency_ms"] == 5100
    assert row["ok"] == 1
    assert row["error"] == ""
    assert row["called_at"]


def test_a_failed_call_is_recorded_too(logged: sqlite3.Connection) -> None:
    """실패한 호출도 토큰을 쓴다. 빼고 세면 합이 실제와 어긋난다."""
    record_call(
        logged,
        feature=CLASSIFY,
        usage=USAGE_ONE,
        ok=False,
        error="Gemini 호출 실패(429): 분당 한도 초과",
    )

    row = logged.execute("SELECT ok, error FROM llm_calls").fetchone()
    assert row["ok"] == 0
    assert "429" in row["error"]


def test_the_totals_add_up_to_what_was_recorded(logged: sqlite3.Connection) -> None:
    record_call(logged, feature=CLASSIFY, usage=USAGE_ONE)
    record_call(logged, feature=CLASSIFY, usage=USAGE_TWO)
    record_call(logged, feature=SELECTOR_GENERATE, usage=USAGE_TWO)

    everything = totals(logged)
    assert everything["calls"] == 3
    assert everything["total_tokens"] == 4441 + 1050 + 1050

    only_classify = totals(logged, CLASSIFY)
    assert only_classify["calls"] == 2
    assert only_classify["input_tokens"] == 4321 + 1000
    assert only_classify["output_tokens"] == 120 + 50
    assert only_classify["latency_ms"] == 5100 + 900


def test_by_feature_lists_every_feature_even_with_no_calls(logged: sqlite3.Connection) -> None:
    """대시보드 그래프에서 아예 빠지면 그 기능이 있는지도 알 수 없다."""
    record_call(logged, feature=CLASSIFY, usage=USAGE_ONE)
    record_call(logged, feature=CLASSIFY, usage=USAGE_TWO)
    record_call(logged, feature=SELECTOR_GENERATE, usage=USAGE_TWO)

    usage = {u.feature: u for u in by_feature(logged)}

    assert set(usage) == {SELECTOR_GENERATE, SELECTOR_REPAIR, CLASSIFY, IMAGE_READ}
    assert usage[CLASSIFY].calls == 2
    assert usage[CLASSIFY].total_tokens == 4441 + 1050
    assert usage[SELECTOR_GENERATE].calls == 1
    assert usage[SELECTOR_REPAIR].calls == 0
    assert usage[SELECTOR_REPAIR].total_tokens == 0
    assert usage[IMAGE_READ].calls == 0


ON_QWEN = Usage(
    provider="qwen",
    model="qwen3.8-flash",
    input_tokens=3000,
    output_tokens=90,
    total_tokens=3090,
    latency_ms=2200,
)


def test_the_provider_that_answered_is_the_one_that_gets_recorded(
    logged: sqlite3.Connection,
) -> None:
    """제공자별로 갈라 세어야 "어느 제공자에 돈이 나갔나" 에 답할 수 있다.

    상수를 박고 있을 때는 둘을 넣어도 한쪽 이름으로만 쌓였다.
    """
    record_call(logged, feature=CLASSIFY, usage=USAGE_ONE)
    record_call(logged, feature=CLASSIFY, usage=ON_QWEN)
    record_call(logged, feature=CLASSIFY, usage=ON_QWEN)

    counted = dict(
        logged.execute("SELECT provider, count(*) FROM llm_calls GROUP BY provider").fetchall()
    )

    assert counted == {"gemini": 1, "qwen": 2}


def test_each_provider_keeps_its_own_model_and_tokens(logged: sqlite3.Connection) -> None:
    """제공자마다 모델도 단가도 다르다. 한 칸에 섞이면 비용을 나눌 수 없다."""
    record_call(logged, feature=CLASSIFY, usage=USAGE_ONE)
    record_call(logged, feature=CLASSIFY, usage=ON_QWEN)

    rows = {
        row["provider"]: (row["model"], row["total_tokens"])
        for row in logged.execute("SELECT provider, model, total_tokens FROM llm_calls")
    }

    assert rows["gemini"] == ("gemini-3.5-flash", 4441)
    assert rows["qwen"] == ("qwen3.8-flash", 3090)


def test_a_failure_to_record_does_not_become_a_failure_to_classify(
    tmp_path: pathlib.Path,
) -> None:
    """표가 없어도 분류는 계속 가야 한다. 남기지 못한 것은 숫자 한 줄이다."""
    connection = db.connect(tmp_path / "no-schema.db")
    try:
        assert record_call(connection, feature=CLASSIFY, usage=USAGE_ONE) == 0
    finally:
        connection.close()


def test_registering_a_crawler_records_its_generation_call(
    client: TestClient,  # noqa: F811
    conn: sqlite3.Connection,  # noqa: F811
) -> None:
    """지금까지 비싼 호출이던 쪽도 같은 표에 남는다. 둘을 견줄 수 있어야 한다."""
    use_generator(result_for(GENERATED))

    response = client.post(
        "/api/crawlers",
        json={"name": "파이썬", "list_url": LIST_URL, "detail_url": DETAIL_URL},
    )

    assert response.status_code == 201
    row = conn.execute("SELECT feature, model, total_tokens FROM llm_calls").fetchone()
    assert row["feature"] == SELECTOR_GENERATE
    assert row["model"] == USAGE.model
    assert row["total_tokens"] == USAGE.total_tokens
