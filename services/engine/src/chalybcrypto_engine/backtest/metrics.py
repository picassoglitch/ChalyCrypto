"""Backtest performance summary — all on NET PnL.

CLAUDE.md style rule: backtests are labelled OPTIMISTIC. The label travels with the result
all the way to the UI/db (ARCHITECTURE §0.6).
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from .fills import SimulatedTrade


_FROZEN = ConfigDict(extra="forbid", frozen=True)
_BPS = Decimal("10000")


class BacktestMetrics(BaseModel):
    model_config = _FROZEN

    sample_size: int
    win_rate: Decimal
    avg_win_bps: Decimal
    avg_loss_bps: Decimal  # magnitude, positive
    profit_factor: Decimal | None
    avg_rr: Decimal | None
    max_drawdown_bps: Decimal
    fee_drag_bps: Decimal
    optimistic: bool = True  # always True for a backtest


def summarize(trades: list[SimulatedTrade], *, starting_equity: Decimal) -> BacktestMetrics:
    if not trades:
        return BacktestMetrics(
            sample_size=0,
            win_rate=Decimal(0),
            avg_win_bps=Decimal(0),
            avg_loss_bps=Decimal(0),
            profit_factor=None,
            avg_rr=None,
            max_drawdown_bps=Decimal(0),
            fee_drag_bps=Decimal(0),
        )

    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]

    sample_size = len(trades)
    win_rate = Decimal(len(wins)) / Decimal(sample_size)

    avg_win_bps = (
        sum((t.net_pnl_bps_of_notional for t in wins), start=Decimal(0)) / Decimal(len(wins))
        if wins
        else Decimal(0)
    )
    avg_loss_bps = (
        -sum((t.net_pnl_bps_of_notional for t in losses), start=Decimal(0)) / Decimal(len(losses))
        if losses
        else Decimal(0)
    )

    gross_wins = sum((t.net_pnl for t in wins), start=Decimal(0))
    gross_losses_abs = -sum((t.net_pnl for t in losses), start=Decimal(0))
    profit_factor: Decimal | None
    if gross_losses_abs > 0:
        profit_factor = gross_wins / gross_losses_abs
    else:
        profit_factor = None  # avoid /0 — undefined when there are no losses

    avg_rr = (
        avg_win_bps / avg_loss_bps if avg_loss_bps > 0 else None
    )

    # equity-curve drawdown from trade-time NET PnL
    eq = starting_equity
    peak = starting_equity
    max_dd_bps = Decimal(0)
    for t in trades:
        eq += t.net_pnl
        if eq > peak:
            peak = eq
        if peak > 0 and eq < peak:
            dd_bps = (peak - eq) / peak * _BPS
            if dd_bps > max_dd_bps:
                max_dd_bps = dd_bps

    total_notional = sum((t.entry_notional for t in trades), start=Decimal(0))
    total_fees = sum((t.total_fees + t.funding_paid for t in trades), start=Decimal(0))
    fee_drag_bps = (total_fees / total_notional * _BPS) if total_notional > 0 else Decimal(0)

    return BacktestMetrics(
        sample_size=sample_size,
        win_rate=win_rate,
        avg_win_bps=avg_win_bps,
        avg_loss_bps=avg_loss_bps,
        profit_factor=profit_factor,
        avg_rr=avg_rr,
        max_drawdown_bps=max_dd_bps,
        fee_drag_bps=fee_drag_bps,
    )
