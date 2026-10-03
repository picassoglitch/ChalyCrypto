"""Durable usage outbox: events are written locally first, then drained to the hub.

Consumption contract, "Delivery": events must survive restarts and scale-to-zero,
so callers enqueue into a table (PgStore implements `OutboxStore`) and a drainer
ships them to POST /usage in batches of at most 100 with backoff.

  2xx                   → rows marked sent
  408 / 429 / 5xx / net → rows rescheduled with exponential backoff
  any other 4xx         → rows marked dead + logged at ERROR (never dropped)

(engine, source_id) is unique on the hub, so re-sending after a crash between the
POST and the mark-sent is safe.
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from .client import MAX_EVENTS_PER_REQUEST, HubClient, HubRejected, HubUnavailable
from .pricing import llm_cost_usd_micros, normalize_model

log = logging.getLogger(__name__)

BACKOFF_BASE_SECONDS = 5.0
BACKOFF_MAX_SECONDS = 3600.0


@dataclass(frozen=True)
class UsageEvent:
    external_user_id: str
    kind: str
    amount: int
    cost_usd_micros: int
    provider: str | None = None
    operation: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    reservation_id: str | None = None
    source_id: str = field(default_factory=lambda: f"evt_{uuid.uuid4().hex}")
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_wire(self) -> dict[str, Any]:
        """Body of one entry in POST /usage `events`."""
        out: dict[str, Any] = {
            "kind": self.kind,
            "amount": int(self.amount),
            "cost_usd_micros": int(self.cost_usd_micros),
            "source_id": self.source_id,
            "occurred_at": self.occurred_at.astimezone(UTC).isoformat(),
        }
        if self.provider:
            out["provider"] = self.provider
        if self.operation:
            out["operation"] = self.operation
        if self.metadata:
            out["metadata"] = self.metadata
        if self.reservation_id:
            out["reservation_id"] = self.reservation_id
        return out


@dataclass(frozen=True)
class OutboxRow:
    id: Any
    external_user_id: str
    event: dict[str, Any]  # wire shape, as produced by UsageEvent.to_wire()
    attempts: int = 0


class OutboxStore(Protocol):
    async def enqueue_usage_events(self, events: list[UsageEvent]) -> int: ...

    async def claim_usage_outbox(self, *, limit: int, lease_seconds: int) -> list[OutboxRow]:
        """Return up to `limit` due pending rows and push their next_attempt_at out by
        `lease_seconds` so a concurrent drainer doesn't pick them up too."""
        ...

    async def mark_usage_sent(self, ids: list[Any]) -> None: ...

    async def mark_usage_retry(self, retries: list[tuple[Any, float]], *, error: str) -> None:
        """retries = [(row_id, delay_seconds)]; increments attempts."""
        ...

    async def mark_usage_dead(self, ids: list[Any], *, error: str) -> None: ...


def llm_tokens_event(
    *,
    external_user_id: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    operation: str | None = None,
    reservation_id: str | None = None,
    provider: str = "anthropic",
    extra_metadata: dict[str, Any] | None = None,
) -> UsageEvent:
    """Build an `llm.tokens` event with the contract's metadata.tokens split and the
    priced cost. `amount` is the total of all four token buckets."""
    tokens = {
        "input": int(input_tokens),
        "output": int(output_tokens),
        "cache_read": int(cache_read_tokens),
        "cache_write": int(cache_write_tokens),
    }
    metadata: dict[str, Any] = {"model": normalize_model(model), "tokens": tokens}
    if extra_metadata:
        metadata.update(extra_metadata)
    return UsageEvent(
        external_user_id=external_user_id,
        kind="llm.tokens",
        amount=sum(tokens.values()),
        cost_usd_micros=llm_cost_usd_micros(
            model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        ),
        provider=provider,
        operation=operation,
        metadata=metadata,
        reservation_id=reservation_id,
        source_id=f"llm_{uuid.uuid4().hex}",
    )


def backoff_seconds(attempts: int, *, jitter: bool = True) -> float:
    """Delay before the next try, given how many attempts have already failed."""
    delay = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1)))
    if jitter:
        delay *= random.uniform(0.8, 1.2)
    return delay


@dataclass
class DrainStats:
    claimed: int = 0
    sent: int = 0
    retried: int = 0
    dead: int = 0


async def _send_group(
    store: OutboxStore, client: HubClient, user_id: str, rows: list[OutboxRow], stats: DrainStats
) -> None:
    try:
        await client.post_usage(external_user_id=user_id, events=[r.event for r in rows])
    except HubUnavailable as e:
        await store.mark_usage_retry(
            [(r.id, backoff_seconds(r.attempts + 1)) for r in rows], error=str(e)
        )
        stats.retried += len(rows)
        return
    except HubRejected as e:
        # The hub rejects the whole request for one bad event. Split so one poison
        # row doesn't dead-letter its neighbours — unless the error is about the
        # request itself (auth / unknown user), where every row gets the same answer.
        if len(rows) > 1 and e.status_code not in (401, 403, 404):
            for r in rows:
                await _send_group(store, client, user_id, [r], stats)
            return
        log.error(
            "usage outbox: hub permanently rejected %d event(s) for user %s: %s",
            len(rows),
            user_id,
            e,
        )
        await store.mark_usage_dead([r.id for r in rows], error=str(e))
        stats.dead += len(rows)
        return
    await store.mark_usage_sent([r.id for r in rows])
    stats.sent += len(rows)


async def drain_outbox(
    store: OutboxStore,
    client: HubClient,
    *,
    batch_size: int = MAX_EVENTS_PER_REQUEST,
    lease_seconds: int = 120,
) -> DrainStats:
    """Claim one batch (≤100 rows) and ship it, grouped per user (/usage takes a
    single external_user_id per request)."""
    stats = DrainStats()
    rows = await store.claim_usage_outbox(
        limit=max(1, min(batch_size, MAX_EVENTS_PER_REQUEST)), lease_seconds=lease_seconds
    )
    stats.claimed = len(rows)
    groups: dict[str, list[OutboxRow]] = defaultdict(list)
    for r in rows:
        groups[r.external_user_id].append(r)
    for user_id, group in groups.items():
        await _send_group(store, client, user_id, group, stats)
    return stats


async def run_drain_loop(
    store: OutboxStore,
    client: HubClient,
    *,
    interval_seconds: float = 15.0,
    stop: asyncio.Event | None = None,
) -> None:
    """Drain forever: keep pulling full batches back-to-back, then idle."""
    stop = stop or asyncio.Event()
    while not stop.is_set():
        try:
            while True:
                stats = await drain_outbox(store, client)
                if stats.claimed < MAX_EVENTS_PER_REQUEST or stats.sent == 0:
                    break
        except Exception:  # never let the drainer die; rows stay pending
            log.exception("usage outbox: drain pass failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
