"""Chalyb hub consumption client: admit/settle/usage, price table, usage outbox.

Shared by the API service (admission gate + outbox drainer) and the LLM service
(per-call `llm.tokens` metering). Contract: chalyb docs/engines/consumption-contract.md.
"""

from .client import (
    MAX_EVENTS_PER_REQUEST,
    AdmitResult,
    HubClient,
    HubError,
    HubRejected,
    HubUnavailable,
)
from .outbox import (
    DrainStats,
    OutboxRow,
    OutboxStore,
    UsageEvent,
    backoff_seconds,
    drain_outbox,
    llm_tokens_event,
    run_drain_loop,
)
from .pricing import ANTHROPIC_PRICES, ModelPrice, llm_cost_usd_micros, normalize_model

__all__ = [
    "ANTHROPIC_PRICES",
    "MAX_EVENTS_PER_REQUEST",
    "AdmitResult",
    "DrainStats",
    "HubClient",
    "HubError",
    "HubRejected",
    "HubUnavailable",
    "ModelPrice",
    "OutboxRow",
    "OutboxStore",
    "UsageEvent",
    "backoff_seconds",
    "drain_outbox",
    "llm_cost_usd_micros",
    "llm_tokens_event",
    "normalize_model",
    "run_drain_loop",
]
