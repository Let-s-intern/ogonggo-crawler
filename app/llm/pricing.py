"""AI 모델 단가. 대시보드의 예상 비용이 쓴다 (2026-09-15 결정: 코드에 단가표를 둔다).

100만 토큰당 달러, 표준 요금(배치·캐시 할인 없음)이다. 2026-09-15 에 확인했다.

| 제공자 | 출처 |
|---|---|
| Gemini | ai.google.dev/gemini-api/docs/pricing (2026-09-11 갱신분) |
| Claude | platform.claude.com/docs/en/about-claude/pricing |
| GPT | developers.openai.com/api/docs/pricing, 짧은 문맥 요금 |
| Qwen | alibabacloud.com Model Studio 싱가포르 요금 (2026-09-14 갱신분) |

ollama 는 구독 요금이라 토큰 단가가 없다. 표에 없는 모델의 호출은 비용에서 빠지고, 화면이 그 호출
수를 따로 적는다. 가격이 바뀌면 이 표를 고친다.

## 화면에서 덮어쓰기 (2026-09-17 결정, LC-3344)

코드 표가 기본이고, 화면에서 넣은 값이 있으면 그 값이 이긴다. 순서는 코드 표 → 화면에서 추가한
회사 정의의 단가(`app/llm/custom.py`) → 설정 > AI 에서 모델마다 넣은 단가다. 새 모델이 나올 때마다
배포하지 않아도 비용 화면이 맞게 한다. 넣은 단가는 `app_settings` 의 `model_price:{모델}` 행이다.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

# 원/달러 어림값. 정확한 청구액이 아니라 요즘 대략 얼마 나오는지 보는 용도다
KRW_PER_USD = 1380


@dataclass(frozen=True)
class Price:
    input_usd: float
    output_usd: float


PRICES: dict[str, Price] = {
    "gemini-3.5-flash": Price(1.50, 9.00),
    "gemini-3.5-flash-lite": Price(0.30, 2.50),
    "gemini-3.1-flash-lite": Price(0.25, 1.50),
    "claude-haiku-4-5": Price(1.00, 5.00),
    "claude-haiku-4-5-20251001": Price(1.00, 5.00),
    "gpt-5.6-luna": Price(0.20, 1.20),
    "qwen3.8-flash": Price(0.15, 0.47),
}


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """호출 묶음 하나의 비용. 단가를 모르는 모델이면 None 이다."""
    price = PRICES.get(model.strip())
    if price is None:
        return None
    return (input_tokens * price.input_usd + output_tokens * price.output_usd) / 1_000_000


SOURCE_CODE = "코드 표"
SOURCE_CUSTOM = "회사 정의"
SOURCE_STORED = "직접 입력"

PRICE_PREFIX = "model_price:"
_CUSTOM_PREFIX = "llm_custom_"


@dataclass(frozen=True)
class KnownPrice:
    price: Price
    source: str


def price_table(conn: sqlite3.Connection) -> dict[str, KnownPrice]:
    """모델 → 단가와 그 출처. 뒤에 오는 것이 앞의 것을 덮는다 (모듈 설명의 순서)."""
    table = {model: KnownPrice(price, SOURCE_CODE) for model, price in PRICES.items()}
    rows = conn.execute(
        "SELECT key, value FROM app_settings WHERE key LIKE ? OR key LIKE ?",
        (f"{_CUSTOM_PREFIX}%", f"{PRICE_PREFIX}%"),
    ).fetchall()
    stored: dict[str, KnownPrice] = {}
    for row in rows:
        key, raw = str(row["key"]), str(row["value"])
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if key.startswith(_CUSTOM_PREFIX):
            for model, value in (data.get("prices") or {}).items():
                price = _price(value)
                if price is not None:
                    table[str(model)] = KnownPrice(price, SOURCE_CUSTOM)
        else:
            price = _price(data)
            if price is not None:
                stored[key[len(PRICE_PREFIX) :]] = KnownPrice(price, SOURCE_STORED)
    table.update(stored)
    return table


def cost_in(
    table: dict[str, KnownPrice], model: str, input_tokens: int, output_tokens: int
) -> float | None:
    """`price_table` 로 센 비용. 단가를 모르는 모델이면 None 이다."""
    known = table.get(model.strip())
    if known is None:
        return None
    price = known.price
    return (input_tokens * price.input_usd + output_tokens * price.output_usd) / 1_000_000


def save_price(conn: sqlite3.Connection, model: str, input_usd: float, output_usd: float) -> None:
    """화면에서 넣은 단가를 저장한다. 같은 모델이면 고친다."""
    if input_usd < 0 or output_usd < 0:
        raise ValueError("단가는 0 이상이어야 한다")
    conn.execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?)"
        " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (f"{PRICE_PREFIX}{model.strip()}", json.dumps({"input": input_usd, "output": output_usd})),
    )


def delete_price(conn: sqlite3.Connection, model: str) -> None:
    """화면에서 넣은 단가를 지운다. 코드 표나 회사 정의의 단가로 돌아간다."""
    conn.execute("DELETE FROM app_settings WHERE key = ?", (f"{PRICE_PREFIX}{model.strip()}",))


def _price(value: object) -> Price | None:
    if not isinstance(value, dict):
        return None
    try:
        input_usd, output_usd = float(value["input"]), float(value["output"])
    except (KeyError, TypeError, ValueError):
        return None
    if input_usd < 0 or output_usd < 0:
        return None
    return Price(input_usd, output_usd)
