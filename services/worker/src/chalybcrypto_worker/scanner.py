"""Scanner — one tick of the hot path, against a real venue snapshot.

Pulls live klines + funding + mark price from the venue (Bitunix by default), builds a
MarketSnapshot, runs every configured strategy, routes each emitted Signal through the
RiskEngine in PAPER mode, and returns a structured report.

This is the orchestration glue per ARCHITECTURE §2 — pure functions over the inputs;
the only I/O is the venue snapshot fetch (which the caller can mock). No LLM calls,
no order placement, no state outside the returned ScanResult.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Awaitable, Callable, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from chalybcrypto_engine.risk import (
    AccountState,
    InMemoryIdempotencyStore,
    RiskEngine,
    StrategyStats,
)
from chalybcrypto_engine.risk.ev import EVInputs
from chalybcrypto_engine.strategy import (
    EmaAdxTrendParams,
    EmaAdxTrendStrategy,
    FvgObParams,
    FvgObStrategy,
    StrategyContext,
    VwapRsiMeanRevParams,
    VwapRsiMeanRevStrategy,
)
from chalybcrypto_engine.strategy.base import Strategy, StrategyParams
from chalybcrypto_shared import (
    FeeSchedule,
    Kline,
    MarketSnapshot,
    Mode,
    RiskProfile,
    Signal,
    TradeDecision,
)


_FROZEN = ConfigDict(extra="forbid", frozen=True)


class KlineSource(Protocol):
    """Anything that can hand back klines + (optionally) funding/mark. Lets tests inject
    a fake venue without spinning real HTTP."""

    async def klines(self, pair: str, interval: str, *, limit: int = 100) -> list[Kline]: ...
    async def funding(self, pair: str): ...


class StoreLike(Protocol):
    """Subset of ApiStore the scanner persists to. Both InMemoryStore and PgStore satisfy."""

    async def add_parsed_signal(self, *, user_id: UUID, signal, raw_text: str | None = None) -> dict: ...
    async def add_validated_signal(self, *, user_id: UUID, decision) -> dict: ...
    async def add_approval(self, *, user_id: UUID, signal, decision) -> dict: ...


@dataclass(frozen=True)
class StrategyOutcome:
    strategy_key: str
    signal: Signal | None
    decision: TradeDecision | None  # None when no signal fired

    @property
    def fired(self) -> bool:
        return self.signal is not None

    @property
    def approved(self) -> bool:
        return self.decision is not None and self.decision.approved


class ScanResult(BaseModel):
    model_config = _FROZEN

    pair: str
    taken_at: datetime
    kline_count: int
    mark_price: Decimal
    funding_rate: Decimal
    outcomes: list[StrategyOutcome] = []
    notes: list[str] = []

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)


def default_strategies() -> list[tuple[Strategy, StrategyParams]]:
    """MVP three (ARCHITECTURE §3). Tuned with permissive thresholds so the demo
    actually surfaces evaluations even on quiet markets."""
    return [
        (EmaAdxTrendStrategy(), EmaAdxTrendParams(adx_threshold=Decimal("18"))),
        (VwapRsiMeanRevStrategy(), VwapRsiMeanRevParams(adx_ceiling=Decimal("25"))),
        (FvgObStrategy(), FvgObParams(min_gap_bps=Decimal("8"), min_impulse_bps=Decimal("25"))),
    ]


def _default_account(*, now: datetime) -> AccountState:
    return AccountState(
        equity=Decimal("10000"),
        balance=Decimal("10000"),
        peak_equity=Decimal("10000"),
        last_tick_at=now,
        paper_gate_unlocked=True,
    )


def _default_risk_profile() -> RiskProfile:
    return RiskProfile(
        name="scanner_default",
        max_risk_per_trade_bps=Decimal("50"),
        max_daily_loss_bps=Decimal("300"),
        max_weekly_loss_bps=Decimal("800"),
        max_drawdown_bps=Decimal("1500"),
        max_open_positions=3,
        max_leverage=Decimal("20"),
        max_exposure_per_asset_bps=Decimal("3000"),
        max_total_exposure_bps=Decimal("8000"),
        max_trades_per_hour=6,
        min_rr=Decimal("1.4"),
        min_adx=Decimal("18"),
        min_liquidity_usd=Decimal("0"),
        min_volume_usd=Decimal("0"),
        min_expected_profit_after_fees_bps=Decimal("5"),
        min_liquidation_distance_bps=Decimal("200"),
        stale_price_max_seconds=120,
        cooldown_after_loss_seconds=900,
        cooldown_after_volatility_spike_seconds=300,
        breakeven_trigger_bps=Decimal("30"),
        trailing_trigger_bps=Decimal("60"),
        partial_tp_trigger_bps=Decimal("40"),
    )


def _default_ev_inputs(funding_rate: Decimal, *, now: datetime) -> EVInputs:
    fee = FeeSchedule(
        exchange="bitunix", symbol=None, vip_level="VIP0",
        maker_bps=Decimal("2"), taker_bps=Decimal("6"),
        effective_at=now, source="scanner",
    )
    return EVInputs(
        fee_schedule=fee,
        use_taker_both_sides=True,
        expected_spread_bps=Decimal("1"),
        expected_slippage_bps=Decimal("1"),
        expected_hold_hours=Decimal("0.5"),
        funding_rate=funding_rate,
        funding_interval_hours=8,
    )


async def scan_once(
    source: KlineSource,
    pair: str,
    *,
    interval: str = "5m",
    bars: int = 300,
    strategies: list[tuple[Strategy, StrategyParams]] | None = None,
    risk_profile: RiskProfile | None = None,
    account: AccountState | None = None,
    mode: Mode = Mode.PAPER,
    strategy_stats: dict[str, StrategyStats] | None = None,
    now: datetime | None = None,
    store: StoreLike | None = None,
    user_id: UUID | None = None,
) -> ScanResult:
    """Run one scan tick: snapshot → strategies → risk → ScanResult.

    All inputs override-able; defaults are tuned for paper-mode demo use.
    """
    ts = now or datetime.now(timezone.utc)

    klines = await source.klines(pair, interval, limit=bars)
    funding = await source.funding(pair)

    snapshot = MarketSnapshot(
        pair=pair,
        exchange="bitunix",
        taken_at=ts,
        klines=klines,
        mark_price=funding.mark_price,
        funding_rate=funding.funding_rate,
    )

    strats = strategies or default_strategies()
    profile = risk_profile or _default_risk_profile()
    acct = account or _default_account(now=ts)
    ev_inputs = _default_ev_inputs(funding.funding_rate, now=ts)
    eng = RiskEngine()
    idem = InMemoryIdempotencyStore()

    outcomes: list[StrategyOutcome] = []
    for strat, params in strats:
        sig = strat.evaluate(snapshot, params, StrategyContext(now=ts))
        if sig is None:
            outcomes.append(StrategyOutcome(strategy_key=strat.key, signal=None, decision=None))
            continue
        stats = (strategy_stats or {}).get(strat.key)
        decision = await eng.authorize_new_entry(
            signal=sig,
            account=acct,
            risk_profile=profile,
            ev_inputs=ev_inputs,
            strategy_stats=stats,
            idempotency_store=idem,
            mode=mode,
            now=ts,
        )
        outcomes.append(StrategyOutcome(strategy_key=strat.key, signal=sig, decision=decision))

        if store is not None and user_id is not None:
            await store.add_parsed_signal(user_id=user_id, signal=sig)
            await store.add_validated_signal(user_id=user_id, decision=decision)
            # Semi-auto: APPROVED entries queue for human review before any exchange
            # write. Paper mode auto-simulates fills; full_auto is forbidden in MVP
            # (CLAUDE.md rule 5). Rejected signals never enter the queue.
            if mode == Mode.SEMI_AUTO and decision.approved:
                await store.add_approval(user_id=user_id, signal=sig, decision=decision)

    return ScanResult(
        pair=pair,
        taken_at=ts,
        kline_count=len(klines),
        mark_price=funding.mark_price,
        funding_rate=funding.funding_rate,
        outcomes=outcomes,
        notes=[],
    )
