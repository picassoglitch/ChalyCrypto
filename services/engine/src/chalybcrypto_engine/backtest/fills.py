"""Conservative fill simulator.

ARCHITECTURE §0.6: naive OHLCV backtests lie. This model deliberately makes scalp-style
trades look *worse* than they would in a perfect world:

  * Both sides charged taker fees by default.
  * Entry filled at next-bar-open ± half-spread ± slippage (against you).
  * Exit at SL/TP filled at the stop price ± slippage (against you).
  * Funding accrues on every full funding-interval crossed while the position is open.

Everything is Decimal. Caller decides slippage/spread; this module never guesses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from chalybcrypto_shared import FeeSchedule, Kline, Side


_FROZEN = ConfigDict(extra="forbid", frozen=True)
_BPS = Decimal("10000")


class ConservativeFillModel(BaseModel):
    """Static per-backtest config — fee schedule + slippage/spread/funding params."""

    model_config = _FROZEN

    fee_schedule: FeeSchedule
    spread_bps: Decimal = Decimal("1")
    slippage_bps: Decimal = Decimal("1")
    use_taker_both_sides: bool = True
    funding_rate_per_interval: Decimal = Decimal("0")
    funding_interval_hours: int = 8


@dataclass(frozen=True)
class FillContext:
    """Per-fill state. `bar` is the kline being filled into."""

    bar: Kline


class SimulatedTrade(BaseModel):
    model_config = _FROZEN

    pair: str
    side: Side
    qty: Decimal
    entry_price: Decimal
    exit_price: Decimal
    entry_fee: Decimal
    exit_fee: Decimal
    funding_paid: Decimal
    slippage_cost: Decimal
    opened_at: datetime
    closed_at: datetime
    exit_reason: str  # 'tp' | 'sl' | 'forced_close' | 'time_stop'

    @property
    def gross_pnl(self) -> Decimal:
        if self.side == Side.LONG:
            return (self.exit_price - self.entry_price) * self.qty
        return (self.entry_price - self.exit_price) * self.qty

    @property
    def total_fees(self) -> Decimal:
        return self.entry_fee + self.exit_fee

    @property
    def net_pnl(self) -> Decimal:
        return self.gross_pnl - self.total_fees - self.funding_paid

    @property
    def entry_notional(self) -> Decimal:
        return self.entry_price * self.qty

    @property
    def net_pnl_bps_of_notional(self) -> Decimal:
        n = self.entry_notional
        return (self.net_pnl / n) * _BPS if n > 0 else Decimal(0)


def _entry_price_with_cost(reference: Decimal, model: ConservativeFillModel, side: Side) -> tuple[Decimal, Decimal]:
    """Move the reference price against the trader by half-spread + slippage. Returns
    (effective_price, slippage_cost_in_price_units)."""
    cost_frac = (model.spread_bps / Decimal("2") + model.slippage_bps) / _BPS
    delta = reference * cost_frac
    if side == Side.LONG:
        return (reference + delta, delta)
    return (reference - delta, delta)


def _exit_price_with_cost(reference: Decimal, model: ConservativeFillModel, side: Side) -> tuple[Decimal, Decimal]:
    """Exiting a long means selling — slippage takes the price down. Mirror for short."""
    cost_frac = (model.spread_bps / Decimal("2") + model.slippage_bps) / _BPS
    delta = reference * cost_frac
    if side == Side.LONG:
        return (reference - delta, delta)
    return (reference + delta, delta)


def simulate_entry_fill(
    side: Side,
    qty: Decimal,
    bar: Kline,
    model: ConservativeFillModel,
) -> tuple[Decimal, Decimal, Decimal]:
    """Return (effective_entry_price, entry_fee_paid, slippage_cost_per_unit_price)."""
    eff, slip = _entry_price_with_cost(bar.open, model, side)
    fee_bps = model.fee_schedule.taker_bps if model.use_taker_both_sides else model.fee_schedule.maker_bps
    fee = eff * qty * (fee_bps / _BPS)
    return (eff, fee, slip)


def simulate_exit_fill(
    side: Side,
    qty: Decimal,
    reference_price: Decimal,
    bar: Kline,
    model: ConservativeFillModel,
) -> tuple[Decimal, Decimal]:
    """Return (effective_exit_price, exit_fee_paid). reference is SL/TP price or bar.close
    for time-stops. Bar passed in for symmetry/extension; unused here but kept in signature
    so the conservative model can later add intra-bar fill heuristics."""
    eff, _slip = _exit_price_with_cost(reference_price, model, side)
    fee_bps = model.fee_schedule.taker_bps  # always taker on exit by default (stop/market)
    fee = eff * qty * (fee_bps / _BPS)
    return (eff, fee)


def funding_paid_for_hold(
    *,
    notional: Decimal,
    held_hours: Decimal,
    model: ConservativeFillModel,
) -> Decimal:
    """Charge funding on each full interval crossed; round up the count (conservative)."""
    if model.funding_rate_per_interval == 0 or held_hours <= 0 or notional <= 0:
        return Decimal(0)
    cycles_raw = held_hours / Decimal(model.funding_interval_hours)
    whole = int(cycles_raw)
    if cycles_raw > whole:
        whole += 1
    if whole == 0:
        whole = 1  # any hold pays at least one cycle in expectation
    return notional * model.funding_rate_per_interval * Decimal(whole)
