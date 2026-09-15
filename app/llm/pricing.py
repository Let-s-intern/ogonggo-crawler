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
"""

from __future__ import annotations

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
