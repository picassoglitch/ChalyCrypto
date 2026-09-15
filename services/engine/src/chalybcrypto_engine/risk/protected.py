"""Breakeven manager + ratcheting protected-profit stop (ARCHITECTURE §6).

The spec example: "up $100, continue → protect at least $70" — made precise here as a
**net** floor on PnL after the exit taker fee + accrued funding. The floor only ratchets
UP, never down.

Invariants enforced by tests:
  * On every new peak, floor moves up.
  * On a pullback, floor does NOT move down.
  * The amount protected is what actually lands in the account (net of exit fee + funding).
  * Floor never reaches the peak (giveback in (0, 1]).
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from chalybcrypto_shared import RiskProfile, Side

from .types import PositionState


_FROZEN = ConfigDict(extra="forbid", frozen=True)
_BPS = Decimal("10000")


class ProtectedProfitState(BaseModel):
    model_config = _FROZEN

    peak_gain_net: Decimal
    protected_floor_net: Decimal


class BreakevenResult(BaseModel):
    model_config = _FROZEN

    stop_price: Decimal


def gross_unrealized_pnl(position: PositionState, mark: Decimal) -> Decimal:
    """Gross PnL of an open position at `mark` price. Side-aware."""
    if position.side == Side.LONG:
        return (mark - position.entry_price) * position.qty
    return (position.entry_price - mark) * position.qty


def net_unrealized_pnl(
    position: PositionState,
    mark: Decimal,
    *,
    exit_taker_bps: Decimal,
    accrued_funding: Decimal = Decimal("0"),
) -> Decimal:
    """Net PnL: gross minus the exit-side taker fee on current notional minus accrued funding.

    Entry-side fee is already in `position.fees_paid` and gets surfaced via the caller's
    account math, not here. We only deduct what would be paid AT exit if closed now.
    """
    gross = gross_unrealized_pnl(position, mark)
    exit_notional = mark * position.qty
    exit_fee = exit_notional * (exit_taker_bps / _BPS)
    return gross - exit_fee - accrued_funding


def advance_protected_floor(
    *,
    current_net_pnl: Decimal,
    prior_peak_net: Decimal,
    prior_floor_net: Decimal | None,
    giveback: Decimal,
) -> ProtectedProfitState:
    """Ratchet the peak up and recompute the floor. Floor only moves up, never down.

    giveback ∈ (0, 1]. Default per risk profile is 0.30 (give back 30% of peak gain).
    """
    if giveback <= 0 or giveback > 1:
        raise ValueError(f"giveback must be in (0, 1], got {giveback}")

    new_peak = max(prior_peak_net, current_net_pnl)
    candidate_floor = new_peak * (Decimal("1") - giveback)
    if prior_floor_net is None:
        new_floor = candidate_floor
    else:
        new_floor = max(prior_floor_net, candidate_floor)
    return ProtectedProfitState(peak_gain_net=new_peak, protected_floor_net=new_floor)


def should_trigger_protected_exit(
    *,
    current_net_pnl: Decimal,
    protected_floor_net: Decimal | None,
) -> bool:
    """Close when net unrealized touches or falls below the floor."""
    if protected_floor_net is None:
        return False
    # Only protect once we have a positive floor; a negative floor would close at a loss.
    if protected_floor_net <= 0:
        return False
    return current_net_pnl <= protected_floor_net


def breakeven_stop_price(
    *,
    position: PositionState,
    profile: RiskProfile,
    exit_taker_bps: Decimal,
) -> BreakevenResult:
    """When unrealized ≥ breakeven_trigger_bps, move SL to entry + a fees buffer.

    "Breakeven" means net-zero after the exit fee, not gross-zero — otherwise the fee at
    exit pulls the trade slightly negative even though the stop "hit breakeven".
    """
    fee_buffer = position.entry_price * (exit_taker_bps / _BPS)
    if position.side == Side.LONG:
        stop = position.entry_price + fee_buffer
    else:
        stop = position.entry_price - fee_buffer
    return BreakevenResult(stop_price=stop)
