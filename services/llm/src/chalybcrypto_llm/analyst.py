"""ClaudeAnalyst — async wrapper around Anthropic's Messages API.

Calls api.anthropic.com directly via httpx; no Anthropic SDK dependency (keeps the
worker image lean and avoids SDK version churn). Uses prompt caching on the system
prompt so the cache hits across calls of the same kind.

Failure mode: any HTTP/transport error returns None. The engine NEVER awaits an
analyst response before deciding/filling. CLAUDE.md rule 2:

  "If you find yourself awaiting an LLM before a fill, stop — that's the bug."

Metering: when constructed with a `usage_sink` (the durable usage outbox, e.g.
PgStore) and called with an `external_user_id`, every attempt — successful or not
— enqueues one `llm.tokens` event (provider 'anthropic', metadata.tokens split,
cost_usd_micros from chalybcrypto_hub.pricing) for the drainer to ship to the hub.
A metering failure is logged and never changes the analyst's return value.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from chalybcrypto_hub import OutboxStore, llm_tokens_event
from chalybcrypto_shared import MarketSnapshot, Side, Signal, TradeDecision

log = logging.getLogger(__name__)


ANTHROPIC_API_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-haiku-4-5"

_FROZEN = ConfigDict(extra="forbid", frozen=True)


class AnalystResponse(BaseModel):
    """Common envelope around any analyst output."""

    model_config = _FROZEN

    kind: str  # 'thesis' | 'continue_brief' | 'daily_digest'
    model: str
    content: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass(frozen=True)
class TradeThesis:
    signal_id: Any
    pair: str
    side: Side


@dataclass(frozen=True)
class ContinueBriefing:
    pair: str
    side: Side
    current_unrealized_net: Decimal


@dataclass(frozen=True)
class DailyDigest:
    day: datetime
    trade_count: int
    net_pnl: Decimal


# Reusable system prompts. Marked cacheable in the API call so cache hits accrue.

_THESIS_SYSTEM = (
    "You are ChalyCrypto's post-trade explainer. The deterministic engine has ALREADY "
    "approved this trade. Your job is to write a short, sober thesis (3-5 sentences) "
    "explaining WHY the strategy fired and what the operator should watch. "
    "Avoid hype. Avoid guarantees of profit (CLAUDE.md). Spanish unless the user data "
    "is clearly English."
)

_CONTINUE_SYSTEM = (
    "You write 'continue or exit?' briefings for OPEN positions. Risk is already managed "
    "by the engine (breakeven + protected-profit). Your job is to summarize the structure "
    "of the move so far in 2-3 sentences: did the impulse extend, did momentum stall, did "
    "the funding flip. NEVER tell the operator to add size; the engine forbids pyramiding."
)

_DIGEST_SYSTEM = (
    "You write a one-paragraph daily digest for an automated trading operator. Cover: "
    "trade count, win rate at a glance, biggest win, biggest loss, fee drag, any guard "
    "that fired. End with one actionable observation. NEVER reproduce strategy code or "
    "secret keys. Spanish unless data is clearly English."
)


class ClaudeAnalyst:
    """Async, explanation-only Claude wrapper. Returns None on failure (never raises)."""

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str = DEFAULT_MODEL,
        base_url: str = ANTHROPIC_API_BASE,
        max_tokens: int = 512,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
        usage_sink: OutboxStore | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base = base_url.rstrip("/")
        self._max_tokens = max_tokens
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._usage_sink = usage_sink

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key or "",
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    async def _record_usage(
        self,
        *,
        kind: str,
        model: str,
        usage: dict[str, Any],
        status: str,
        external_user_id: str | None,
        reservation_id: str | None,
    ) -> None:
        """Enqueue one llm.tokens event for this attempt. Never raises."""
        if self._usage_sink is None:
            return
        if not external_user_id:
            log.warning("analyst: %s call not metered — no external_user_id", kind)
            return
        try:
            event = llm_tokens_event(
                external_user_id=external_user_id,
                model=model,
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
                cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
                operation=f"analyst.{kind}",
                reservation_id=reservation_id,
                provider="anthropic",
                extra_metadata={"status": status},
            )
            await self._usage_sink.enqueue_usage_events([event])
        except Exception:
            log.exception("analyst: failed to enqueue usage for %s call", kind)

    async def _call(
        self,
        *,
        system: str,
        user: str,
        kind: str,
        external_user_id: str | None = None,
        reservation_id: str | None = None,
    ) -> AnalystResponse | None:
        if not self._api_key:
            return None
        body = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [{"role": "user", "content": user}],
        }
        meter = {
            "kind": kind,
            "external_user_id": external_user_id,
            "reservation_id": reservation_id,
        }
        try:
            r = await self._client.post(
                f"{self._base}/v1/messages",
                headers=self._headers(),
                json=body,
            )
        except httpx.HTTPError as e:
            await self._record_usage(
                model=self._model, usage={}, status=f"transport_error:{type(e).__name__}", **meter
            )
            return None
        try:
            payload = r.json()
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        usage = payload.get("usage", {}) or {}
        model = payload.get("model") or self._model
        if r.status_code >= 400:
            await self._record_usage(
                model=model, usage=usage, status=f"http_{r.status_code}", **meter
            )
            return None
        await self._record_usage(model=model, usage=usage, status="ok", **meter)
        text = ""
        for block in payload.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
        return AnalystResponse(
            kind=kind,
            model=model,
            content=text.strip(),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            cache_read_tokens=int(usage.get("cache_read_input_tokens", 0)),
            cache_creation_tokens=int(usage.get("cache_creation_input_tokens", 0)),
        )

    # ── public methods ────────────────────────────────────────────────────

    async def write_thesis(
        self,
        signal: Signal,
        snapshot: MarketSnapshot,
        decision: TradeDecision,
        *,
        external_user_id: str | None = None,
        reservation_id: str | None = None,
    ) -> AnalystResponse | None:
        user = json.dumps(
            {
                "pair": signal.pair,
                "side": signal.side.value,
                "strategy": signal.strategy,
                "entry": str(signal.entry),
                "stop_loss": str(signal.stop_loss),
                "take_profits": [str(t) for t in signal.take_profits],
                "leverage": str(signal.leverage),
                "timeframe": signal.timeframe,
                "thesis_tags": signal.thesis_tags,
                "ev_net_bps": str(decision.ev_net_bps) if decision.ev_net_bps else None,
                "liquidation_distance_bps": (
                    str(decision.liquidation_distance_bps)
                    if decision.liquidation_distance_bps else None
                ),
                "mark_price": str(snapshot.mark_price) if snapshot.mark_price else None,
                "funding_rate": str(snapshot.funding_rate) if snapshot.funding_rate else None,
            },
            ensure_ascii=False,
        )
        return await self._call(
            system=_THESIS_SYSTEM,
            user=user,
            kind="thesis",
            external_user_id=external_user_id,
            reservation_id=reservation_id,
        )

    async def continue_briefing(
        self,
        brief: ContinueBriefing,
        snapshot: MarketSnapshot,
        *,
        external_user_id: str | None = None,
        reservation_id: str | None = None,
    ) -> AnalystResponse | None:
        user = json.dumps(
            {
                "pair": brief.pair,
                "side": brief.side.value,
                "current_unrealized_net": str(brief.current_unrealized_net),
                "mark_price": str(snapshot.mark_price) if snapshot.mark_price else None,
                "funding_rate": str(snapshot.funding_rate) if snapshot.funding_rate else None,
            },
            ensure_ascii=False,
        )
        return await self._call(
            system=_CONTINUE_SYSTEM,
            user=user,
            kind="continue_brief",
            external_user_id=external_user_id,
            reservation_id=reservation_id,
        )

    async def daily_digest(
        self,
        digest: DailyDigest,
        *,
        external_user_id: str | None = None,
        reservation_id: str | None = None,
    ) -> AnalystResponse | None:
        user = json.dumps(
            {
                "day": digest.day.date().isoformat(),
                "trade_count": digest.trade_count,
                "net_pnl": str(digest.net_pnl),
            },
            ensure_ascii=False,
        )
        return await self._call(
            system=_DIGEST_SYSTEM,
            user=user,
            kind="daily_digest",
            external_user_id=external_user_id,
            reservation_id=reservation_id,
        )
