"""RiskEngine — the final authority on every entry (CLAUDE.md rule 3).

Order of checks is deliberate, cheap-and-fatal first:
  1. account-protection lock
  2. paper gate (for live modes)
  3. stale price
  4. cooldowns
  5. loss guards (daily/weekly/drawdown)
  6. exposure caps (count + rate)
  7. liquidation distance
  8. sizing (leverage, exposure %, RR, min-qty)
  9. EV after costs
 10. idempotency / duplicate signal

Every reject returns a TradeDecision with the reason field set; callers persist that to
audit_logs (CLAUDE.md rule 9). No LLM is consulted anywhere.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from chalybcrypto_shared import (
    Mode,
    OrderType,
    RiskProfile,
    Signal,
    TradeDecision,
    dedup_hash,
)

from .ev import EVInputs, ev_passes
from .guards import (
    check_account_lock,
    check_cooldowns,
    check_exposure_caps,
    check_loss_guards,
    check_paper_gate,
    check_stale_price,
)
from .idempotency import IdempotencyStore
from .liquidation import passes_min_distance
from .sizing import size_position
from .types import AccountState, RejectReason, StrategyStats


IDEMPOTENCY_TTL_SECONDS = 60


class RiskEngine:
    """Stateless orchestrator. All state arrives via parameters."""

    async def authorize_new_entry(
        self,
        *,
        signal: Signal,
        account: AccountState,
        risk_profile: RiskProfile,
        ev_inputs: EVInputs,
        strategy_stats: StrategyStats | None,
        idempotency_store: IdempotencyStore,
        mode: Mode,
        now: datetime | None = None,
    ) -> TradeDecision:
        ts = now or datetime.now(timezone.utc)

        # 1. Account lock — global kill.
        reason = check_account_lock(account)
        if reason != RejectReason.OK:
            return self._reject(signal, mode, ts, reason)

        # 2. Paper gate — live modes only.
        reason = check_paper_gate(account, mode=mode)
        if reason != RejectReason.OK:
            return self._reject(signal, mode, ts, reason)

        # 3. Stale price — unknown last_tick → fail safe.
        reason = check_stale_price(
            account, now=ts, max_seconds=risk_profile.stale_price_max_seconds
        )
        if reason != RejectReason.OK:
            return self._reject(signal, mode, ts, reason)

        # 4. Cooldowns.
        reason = check_cooldowns(account, risk_profile, now=ts)
        if reason != RejectReason.OK:
            return self._reject(signal, mode, ts, reason)

        # 5. Loss guards.
        reason = check_loss_guards(account, risk_profile)
        if reason != RejectReason.OK:
            return self._reject(signal, mode, ts, reason)

        # 6. Exposure caps (count + rate).
        reason = check_exposure_caps(account, risk_profile)
        if reason != RejectReason.OK:
            return self._reject(signal, mode, ts, reason)

        # 7. Liquidation distance.
        liq_ok, liq_price, liq_dist_bps = passes_min_distance(
            side=signal.side,
            entry=signal.entry,
            leverage=min(signal.leverage, risk_profile.max_leverage),
            min_distance_bps=risk_profile.min_liquidation_distance_bps,
        )
        if not liq_ok:
            return self._reject(
                signal,
                mode,
                ts,
                RejectReason.LIQUIDATION_TOO_CLOSE,
                liquidation_price=liq_price,
                liquidation_distance_bps=liq_dist_bps,
            )

        # 8. Sizing.
        size = size_position(signal, account, risk_profile)
        if not size.approved:
            return self._reject(
                signal, mode, ts, size.reject,
                liquidation_price=liq_price,
                liquidation_distance_bps=liq_dist_bps,
            )

        # 9. EV gate.
        ev_ok, ev_reason, ev_bps = ev_passes(
            strategy_stats,
            ev_inputs,
            mode=mode,
            min_expected_profit_after_fees_bps=risk_profile.min_expected_profit_after_fees_bps,
        )
        if not ev_ok:
            return self._reject(
                signal, mode, ts, ev_reason,
                ev_net_bps=ev_bps,
                liquidation_price=liq_price,
                liquidation_distance_bps=liq_dist_bps,
            )

        # 10. Idempotency — atomic claim of dedup_hash. Last gate so we don't burn slots
        # on otherwise-rejected entries.
        idem_key = signal.dedup_hash
        acquired = await idempotency_store.try_acquire(idem_key, IDEMPOTENCY_TTL_SECONDS)
        if not acquired:
            return self._reject(signal, mode, ts, RejectReason.DUPLICATE_SIGNAL)

        # Build the round-trip-fee figure once for the decision record.
        from .ev import round_trip_fees_bps
        fees_rt_bps = round_trip_fees_bps(ev_inputs)

        return TradeDecision(
            signal_id=signal.id,
            mode=mode,
            approved=True,
            reason=RejectReason.OK.value,
            intended_order_type=OrderType.MARKET,
            intended_qty=size.qty,
            intended_entry=signal.entry,
            intended_stop_loss=signal.stop_loss,
            intended_take_profits=signal.take_profits,
            intended_leverage=size.leverage,
            ev_net_bps=ev_bps,
            liquidation_price=liq_price,
            liquidation_distance_bps=liq_dist_bps,
            fees_round_trip_bps=fees_rt_bps,
            idempotency_key=idem_key,
            decided_at=ts,
            actor="risk_engine",
        )

    @staticmethod
    def _reject(
        signal: Signal,
        mode: Mode,
        ts: datetime,
        reason: RejectReason,
        *,
        ev_net_bps: Decimal | None = None,
        liquidation_price: Decimal | None = None,
        liquidation_distance_bps: Decimal | None = None,
    ) -> TradeDecision:
        # Idempotency key on a reject is still required by the model — derive deterministically
        # from the signal + reason so audit-log dedupe is possible.
        idem = dedup_hash(signal.dedup_hash, reason.value)
        return TradeDecision(
            signal_id=signal.id,
            mode=mode,
            approved=False,
            reason=reason.value,
            intended_order_type=None,
            intended_qty=None,
            intended_entry=None,
            intended_stop_loss=None,
            intended_take_profits=[],
            intended_leverage=None,
            ev_net_bps=ev_net_bps,
            liquidation_price=liquidation_price,
            liquidation_distance_bps=liquidation_distance_bps,
            fees_round_trip_bps=None,
            idempotency_key=idem,
            decided_at=ts,
            actor="risk_engine",
        )
