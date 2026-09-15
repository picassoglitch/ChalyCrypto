"""Expected-value gate (ARCHITECTURE §5).

A candidate is eligible only if:

  EV_net = p_win*avg_win - p_loss*avg_loss
         - round_trip_fees(notional, exchange, vip)
         - expected_spread_cost
         - expected_slippage
         - expected_funding(hold_time)
  EV_net > risk_profile.min_expected_profit_after_fees

All terms expressed in **bps of notional** for unit consistency. Stats come from the
strategy's own validated history (StrategyStats). If sample size < threshold, EV is
considered UNKNOWN → reject for live/semi-auto. Backtest/paper may continue to gather
data.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from chalybcrypto_shared import FeeSchedule, Mode

from .types import RejectReason, StrategyStats


_FROZEN = ConfigDict(extra="forbid", frozen=True)


class EVInputs(BaseModel):
    """Per-call cost inputs. Caller supplies measured values; risk engine never guesses."""

    model_config = _FROZEN

    fee_schedule: FeeSchedule
    use_taker_both_sides: bool = True
    expected_spread_bps: Decimal = Decimal("0")
    expected_slippage_bps: Decimal = Decimal("0")
    expected_hold_hours: Decimal = Decimal("0")
    funding_rate: Decimal = Decimal("0")
    funding_interval_hours: int = 8


def round_trip_fees_bps(inputs: EVInputs) -> Decimal:
    """Sum of entry + exit fees in bps of notional. Defaults to taker-taker (conservative)."""
    f = inputs.fee_schedule
    if inputs.use_taker_both_sides:
        return f.taker_bps * Decimal("2")
    # Optimistic: taker entry, maker exit. Reserve for explicit caller choice.
    return f.taker_bps + f.maker_bps


def expected_funding_bps(inputs: EVInputs) -> Decimal:
    """Funding paid over expected hold, in bps of notional.

    Bitunix funding settles every `funding_interval_hours`. Approximation: charges_n =
    ceil(hold / interval). For sub-interval holds we still charge 1 cycle to be safe.
    """
    if inputs.expected_hold_hours <= 0 or inputs.funding_rate == 0:
        return Decimal("0")
    cycles = (inputs.expected_hold_hours / Decimal(inputs.funding_interval_hours))
    # ceil to whole cycles, never optimistic
    whole = int(cycles)
    if cycles > whole:
        whole += 1
    if whole == 0:
        whole = 1  # any hold pays at least one cycle in expectation
    # funding_rate is a fraction (e.g. 0.0001 = 0.01%) → convert to bps
    per_cycle_bps = inputs.funding_rate * Decimal("10000")
    return per_cycle_bps * whole


def ev_net_bps(stats: StrategyStats, inputs: EVInputs) -> Decimal:
    """Compute EV_net per §5, in bps of notional."""
    p_win = stats.win_rate
    p_loss = Decimal("1") - p_win
    gross = p_win * stats.avg_win_bps - p_loss * stats.avg_loss_bps
    costs = (
        round_trip_fees_bps(inputs)
        + inputs.expected_spread_bps
        + inputs.expected_slippage_bps
        + expected_funding_bps(inputs)
    )
    return gross - costs


def ev_passes(
    stats: StrategyStats | None,
    inputs: EVInputs,
    *,
    mode: Mode,
    min_expected_profit_after_fees_bps: Decimal,
) -> tuple[bool, RejectReason, Decimal | None]:
    """Return (approved, reason, ev_bps). Unknown stats → reject for live/semi-auto."""
    if stats is None or stats.sample_size < stats.min_sample_for_live:
        if mode in (Mode.SEMI_AUTO, Mode.FULL_AUTO):
            return (False, RejectReason.EV_STATS_UNKNOWN, None)
        # backtest/paper may continue to gather data
        return (True, RejectReason.OK, None)

    ev = ev_net_bps(stats, inputs)
    if ev <= min_expected_profit_after_fees_bps:
        return (False, RejectReason.EV_NEGATIVE_AFTER_COSTS, ev)
    return (True, RejectReason.OK, ev)
