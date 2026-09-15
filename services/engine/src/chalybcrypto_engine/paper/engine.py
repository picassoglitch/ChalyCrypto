"""Paper engine — same evaluate path as backtest/live, simulated fills, live snapshots.

ARCHITECTURE §2: the hot path is identical across modes; only the fill source differs.
The PaperEngine glues:

    snapshot → strategy.evaluate → EV gate (via RiskEngine) → simulated fill

If the risk engine rejects, the tick records the reason and moves on. Approved entries
get a simulated fill at the next bar's open using ConservativeFillModel. There's no
network call and no LLM call in this loop.

The PaperEngine is stateless per-call; the caller is responsible for tracking open
positions and equity across ticks (the risk engine reads that from AccountState each
tick). This keeps the engine easy to test and matches the §6 pattern of pure functions
over inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from chalybcrypto_shared import (
    Kline,
    MarketSnapshot,
    Mode,
    RiskProfile,
    Signal,
    TradeDecision,
)

from ..backtest.fills import (
    ConservativeFillModel,
    SimulatedTrade,
    simulate_entry_fill,
)
from ..risk import (
    AccountState,
    IdempotencyStore,
    RiskEngine,
    StrategyStats,
)
from ..risk.ev import EVInputs
from ..strategy.base import Strategy, StrategyContext, StrategyParams


_FROZEN = ConfigDict(extra="forbid", frozen=True)


@dataclass(frozen=True)
class PaperTick:
    """One paper-trading evaluation cycle's inputs."""

    snapshot: MarketSnapshot
    next_bar: Kline  # the bar to fill the entry into (next-bar-open)


class PaperTickResult(BaseModel):
    """What one tick produced. Either a SimulatedTrade-in-progress (entry filled, no exit
    yet) or a rejection. Exits are managed by the caller's position tracker the same way
    the backtester does."""

    model_config = _FROZEN

    decision: TradeDecision
    signal: Signal | None = None
    entry_fill: SimulatedTrade | None = None  # exit_price/exit_reason filled at close-time


class PaperEngine:
    def __init__(
        self,
        strategy: Strategy,
        fill_model: ConservativeFillModel,
        *,
        risk_engine: RiskEngine | None = None,
    ) -> None:
        self._strategy = strategy
        self._fill_model = fill_model
        self._risk = risk_engine or RiskEngine()

    async def tick(
        self,
        tick: PaperTick,
        *,
        params: StrategyParams,
        risk_profile: RiskProfile,
        account: AccountState,
        ev_inputs: EVInputs,
        strategy_stats: StrategyStats | None,
        idempotency_store: IdempotencyStore,
        now: datetime | None = None,
    ) -> PaperTickResult:
        ctx = StrategyContext(now=now or datetime.now(timezone.utc))
        sig = self._strategy.evaluate(tick.snapshot, params, ctx)
        if sig is None:
            # No signal — return a synthetic "no entry" decision so audit log can still
            # record the tick. We mark approved=False with reason=ok-but-no-signal via a
            # special idempotency-only payload.
            from chalybcrypto_shared import dedup_hash

            no_sig = TradeDecision(
                signal_id=__import__("uuid").uuid4(),
                mode=Mode.PAPER,
                approved=False,
                reason="no_signal",
                intended_order_type=None,
                intended_qty=None,
                intended_entry=None,
                intended_stop_loss=None,
                intended_take_profits=[],
                intended_leverage=None,
                ev_net_bps=None,
                liquidation_price=None,
                liquidation_distance_bps=None,
                fees_round_trip_bps=None,
                idempotency_key=dedup_hash("no_signal", ctx.now.isoformat(), tick.snapshot.pair),
                decided_at=ctx.now,
                actor="paper_engine",
            )
            return PaperTickResult(decision=no_sig, signal=None, entry_fill=None)

        decision = await self._risk.authorize_new_entry(
            signal=sig,
            account=account,
            risk_profile=risk_profile,
            ev_inputs=ev_inputs,
            strategy_stats=strategy_stats,
            idempotency_store=idempotency_store,
            mode=Mode.PAPER,
            now=ctx.now,
        )
        if not decision.approved or decision.intended_qty is None:
            return PaperTickResult(decision=decision, signal=sig, entry_fill=None)

        # Simulate the entry fill into next_bar.
        eff_entry, entry_fee, slippage = simulate_entry_fill(
            sig.side, decision.intended_qty, tick.next_bar, self._fill_model
        )
        # The caller closes this trade across subsequent ticks; we record exit_price
        # equal to entry_price for now (open position) and tag as 'open'.
        fill = SimulatedTrade(
            pair=sig.pair,
            side=sig.side,
            qty=decision.intended_qty,
            entry_price=eff_entry,
            exit_price=eff_entry,  # placeholder while open
            entry_fee=entry_fee,
            exit_fee=Decimal("0"),
            funding_paid=Decimal("0"),
            slippage_cost=slippage,
            opened_at=tick.next_bar.open_time,
            closed_at=tick.next_bar.open_time,  # placeholder
            exit_reason="open",
        )
        return PaperTickResult(decision=decision, signal=sig, entry_fill=fill)
