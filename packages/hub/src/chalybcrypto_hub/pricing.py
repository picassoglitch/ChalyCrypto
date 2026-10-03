"""Provider price table → cost_usd_micros for hub usage events.

The hub bills every event at real cost (consumption contract, "Billing unit"):
billable_tokens = ceil(cost_usd_micros / 4). So every `llm.tokens` event must carry
a cost computed here, including cache reads/writes and failed attempts.

Unit trick: $X per million tokens == X micro-dollars per token. Rates below are
therefore stored as micros/token and multiplied straight through.

Unknown models are logged at ERROR and priced at the highest known rate, so a
model swap without a table update over-charges rather than giving work away.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from decimal import Decimal

log = logging.getLogger(__name__)

# Anthropic prompt-caching multipliers on the base input rate.
CACHE_READ_MULTIPLIER = Decimal("0.1")
CACHE_WRITE_MULTIPLIER = Decimal("1.25")  # 5-minute ephemeral cache writes


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens (== micros per token)."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal

    @property
    def cache_read_per_mtok(self) -> Decimal:
        return self.input_per_mtok * CACHE_READ_MULTIPLIER

    @property
    def cache_write_per_mtok(self) -> Decimal:
        return self.input_per_mtok * CACHE_WRITE_MULTIPLIER


# Anthropic first-party list prices. claude-haiku-4-5 is the only model the
# analyst uses today; the others are here so a config change to a bigger model
# is priced correctly instead of hitting the unknown-model fallback.
ANTHROPIC_PRICES: dict[str, ModelPrice] = {
    "claude-haiku-4-5": ModelPrice(Decimal("1"), Decimal("5")),
    "claude-sonnet-4-6": ModelPrice(Decimal("3"), Decimal("15")),
    "claude-opus-4-8": ModelPrice(Decimal("5"), Decimal("25")),
}

_DATED_SUFFIX = re.compile(r"-\d{8}$")


def normalize_model(model: str) -> str:
    """'claude-haiku-4-5-20251001' → 'claude-haiku-4-5' (the API echoes dated ids)."""
    return _DATED_SUFFIX.sub("", (model or "").strip().lower())


def _highest_price(table: dict[str, ModelPrice]) -> ModelPrice:
    return ModelPrice(
        max(p.input_per_mtok for p in table.values()),
        max(p.output_per_mtok for p in table.values()),
    )


def price_for(model: str, table: dict[str, ModelPrice] = ANTHROPIC_PRICES) -> ModelPrice:
    key = normalize_model(model)
    price = table.get(key)
    if price is None:
        log.error(
            "pricing: unknown model %r — billing at the highest known rate; add it to the table",
            model,
        )
        return _highest_price(table)
    return price


def llm_cost_usd_micros(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    table: dict[str, ModelPrice] = ANTHROPIC_PRICES,
) -> int:
    """Real provider cost in integer USD micros, rounded up."""
    p = price_for(model, table)
    total = (
        Decimal(max(0, input_tokens)) * p.input_per_mtok
        + Decimal(max(0, output_tokens)) * p.output_per_mtok
        + Decimal(max(0, cache_read_tokens)) * p.cache_read_per_mtok
        + Decimal(max(0, cache_write_tokens)) * p.cache_write_per_mtok
    )
    return int(math.ceil(total))
