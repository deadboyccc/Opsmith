"""Model pricing used for cost accounting and budget enforcement (USD per 1M tokens).

Source: Anthropic first-party API list prices. Update alongside model changes; unknown
models are costed at zero and logged, so budgets should be paired with token caps.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# model -> (input, output, cache_read)
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-fable-5-2": (8.00, 40.00, 0.20),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.10),  # cache-read price unpublished: billed at input rate
    "heuristic-v1": (0.0, 0.0, 0.0),
}


def cost_usd(
    model: str, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0
) -> float:
    price = PRICES.get(model)
    if price is None:
        log.warning("no price for model; cost recorded as 0", extra={"model": model})
        return 0.0
    inp, out, cache = price
    billable_input = max(input_tokens - cache_read_tokens, 0)
    return round(
        (billable_input * inp + output_tokens * out + cache_read_tokens * cache) / 1_000_000, 6
    )
