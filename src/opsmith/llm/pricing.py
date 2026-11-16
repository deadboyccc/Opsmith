"""Model pricing used for cost accounting and budget enforcement (USD per 1M tokens).

Source: Anthropic first-party API list prices. Update alongside model changes. A model id
without a price (a dated or fallback id reported by the API) is never costed at zero: it is
priced as the configured model when that is known, otherwise at the most expensive known rate,
so cost caps cannot be bypassed by an unpriced id.
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


# Every component at its maximum: the conservative price for an unknown model.
CEILING: tuple[float, float, float] = (
    max(p[0] for p in PRICES.values()),
    max(p[1] for p in PRICES.values()),
    max(p[2] for p in PRICES.values()),
)


def price_for(model: str, fallback_model: str | None = None) -> tuple[float, float, float]:
    """Price of ``model``; of ``fallback_model`` (the configured model) when the id the API
    reported is not listed; and the most expensive known price when neither is."""
    price = PRICES.get(model)
    if price is not None:
        return price
    if fallback_model is not None and fallback_model in PRICES:
        log.warning(
            "no price for model; costed as the configured model",
            extra={"model": model, "priced_as": fallback_model},
        )
        return PRICES[fallback_model]
    log.warning("no price for model; costed at the most expensive rate", extra={"model": model})
    return CEILING


def cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    *,
    fallback_model: str | None = None,
) -> float:
    inp, out, cache = price_for(model, fallback_model)
    billable_input = max(input_tokens - cache_read_tokens, 0)
    return round(
        (billable_input * inp + output_tokens * out + cache_read_tokens * cache) / 1_000_000, 6
    )
