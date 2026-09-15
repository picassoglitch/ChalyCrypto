"""Backtester — walks klines, calls strategy.evaluate, simulates fills.

Same evaluate() path runs in backtest/paper/live (ARCHITECTURE §2). Only the fill source
differs. The output is always labelled OPTIMISTIC (CLAUDE.md "keep the UI honest").
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from chalybcrypto_shared import Kline, MarketSnapshot, Side

from ..strategy.base import Strategy, StrategyContext, StrategyParams
from .fills import (
    ConservativeFillModel,
    SimulatedTrade,
    funding_paid_for_hold,
    simulate_entry_fill,
    simulate_exit_fill,
)
from .metrics import BacktestMetrics, summarize


_FROZEN = ConfigDict(extra="forbid", frozen=True)


class BacktestReport(BaseModel):
    model_config = _FROZEN

    strategy_key: str
    pair: str
    timeframe: str
    starting_equity: Decimal
    metrics: BacktestMetrics
    trades: list[SimulatedTrade]
    optimistic: bool = True  # CLAUDE.md: backtests are always labelled OPTIMISTIC

    @property
    def ending_equity(self) -> Decimal:
        return self.starting_equity + sum((t.net_pnl for t in self.trades), start=Decimal(0))


class Backtester:
    """Single-position backtester (no pyramiding). Open positions hold until SL or TP."""

    def __init__(
        self,
        strategy: Strategy,
        fill_model: ConservativeFillModel,
        *,
        starting_equity: Decimal = Decimal("10000"),
        risk_per_trade_bps: Decimal = Decimal("50"),
        leverage: Decimal | None = None,
        bar_hours: Decimal = Decimal("1") / Decimal("12"),  # default 5m
    ) -> None:
        self._strategy = strategy
        self._fill_model = fill_model
        self._starting_equity = starting_equity
        self._risk_per_trade_bps = risk_per_trade_bps
        self._leverage = leverage
        self._bar_hours = bar_hours

    def run(
        self,
        klines: list[Kline],
        params: StrategyParams,
        *,
        pair: str,
        exchange: str = "binance",
        timeframe: str = "5m",
    ) -> BacktestReport:
        trades: list[SimulatedTrade] = []
        i = 0
        n = len(klines)
        while i < n - 1:
            snapshot = MarketSnapshot(
                pair=pair,
                exchange=exchange,
                taken_at=klines[i].close_time,
                klines=klines[: i + 1],
                mark_price=klines[i].close,
            )
            ctx = StrategyContext(now=klines[i].close_time)
            sig = self._strategy.evaluate(snapshot, params, ctx)
            if sig is None:
                i += 1
                continue

            # qty from risk: equity * risk / stop_distance
            stop_dist = abs(sig.entry - sig.stop_loss)
            if stop_dist <= 0:
                i += 1
                continue
            risk_amount = self._starting_equity * (self._risk_per_trade_bps / Decimal("10000"))
            qty = risk_amount / stop_dist
            if qty <= 0:
                i += 1
                continue

            # Open at next bar.
            entry_bar = klines[i + 1]
            eff_entry, entry_fee, _slip = simulate_entry_fill(
                sig.side, qty, entry_bar, self._fill_model
            )
            opened_at = entry_bar.open_time

            # Track until SL/TP hit or end of data.
            tp_price = sig.take_profits[0] if sig.take_profits else None
            sl_price = sig.stop_loss
            exit_idx: int | None = None
            exit_ref: Decimal | None = None
            exit_reason = "forced_close"

            j = i + 1
            while j < n:
                bar = klines[j]
                if sig.side == Side.LONG:
                    hit_sl = bar.low <= sl_price
                    hit_tp = tp_price is not None and bar.high >= tp_price
                else:
                    hit_sl = bar.high >= sl_price
                    hit_tp = tp_price is not None and bar.low <= tp_price
                # Conservative: if a single bar hits both, assume SL was touched first.
                if hit_sl:
                    exit_idx = j
                    exit_ref = sl_price
                    exit_reason = "sl"
                    break
                if hit_tp:
                    exit_idx = j
                    exit_ref = tp_price
                    exit_reason = "tp"
                    break
                j += 1

            if exit_idx is None:
                # Forced-close at last bar's close.
                exit_idx = n - 1
                exit_ref = klines[exit_idx].close
                exit_reason = "forced_close"

            held_hours = self._bar_hours * Decimal(exit_idx - (i + 1) + 1)
            entry_notional = eff_entry * qty
            funding = funding_paid_for_hold(
                notional=entry_notional, held_hours=held_hours, model=self._fill_model
            )
            eff_exit, exit_fee = simulate_exit_fill(
                sig.side, qty, exit_ref, klines[exit_idx], self._fill_model
            )

            trades.append(
                SimulatedTrade(
                    pair=pair,
                    side=sig.side,
                    qty=qty,
                    entry_price=eff_entry,
                    exit_price=eff_exit,
                    entry_fee=entry_fee,
                    exit_fee=exit_fee,
                    funding_paid=funding,
                    slippage_cost=_slip,
                    opened_at=opened_at,
                    closed_at=klines[exit_idx].close_time,
                    exit_reason=exit_reason,
                )
            )

            # Resume scanning AFTER the exit bar.
            i = exit_idx + 1

        metrics = summarize(trades, starting_equity=self._starting_equity)
        return BacktestReport(
            strategy_key=self._strategy.key,
            pair=pair,
            timeframe=timeframe,
            starting_equity=self._starting_equity,
            metrics=metrics,
            trades=trades,
            optimistic=True,
        )
