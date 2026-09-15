from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from chalybcrypto_engine.strategy import (
    EmaAdxTrendParams,
    EmaAdxTrendStrategy,
    StrategyContext,
)
from chalybcrypto_shared import MarketSnapshot, Side

from ._helpers import flat_series, linear_trend, pullback_then_resume_uptrend


def _snapshot(klines):
    return MarketSnapshot(
        pair="BTCUSDT",
        exchange="binance",
        taken_at=klines[-1].close_time,
        klines=klines,
        mark_price=klines[-1].close,
    )


def _ctx():
    return StrategyContext(now=datetime(2026, 6, 6, tzinfo=timezone.utc))


def test_short_series_returns_none():
    strat = EmaAdxTrendStrategy()
    sig = strat.evaluate(
        _snapshot(linear_trend(50, start_price=100, step=1.0)),
        EmaAdxTrendParams(),
        _ctx(),
    )
    assert sig is None  # below min_bars (200)


def test_flat_series_no_signal():
    strat = EmaAdxTrendStrategy()
    sig = strat.evaluate(
        _snapshot(flat_series(220, price=100)),
        EmaAdxTrendParams(),
        _ctx(),
    )
    assert sig is None  # ADX low + no cross


def test_pullback_then_resume_fires_long():
    """A clean uptrend that dips under EMA(fast) then closes back above on the next bar
    should produce a LONG signal on that bar."""
    strat = EmaAdxTrendStrategy()
    series = pullback_then_resume_uptrend()
    sig = strat.evaluate(
        _snapshot(series), EmaAdxTrendParams(adx_threshold=Decimal("15")), _ctx()
    )
    assert sig is not None
    assert sig.side == Side.LONG
    assert sig.entry == series[-1].close
    assert sig.stop_loss < sig.entry
    assert sig.take_profits[0] > sig.entry
    assert "ema_aligned" in sig.thesis_tags
    assert sig.strategy == "ema_adx_trend"
    assert sig.dedup_hash  # populated


def test_signal_carries_atr_based_stop_and_tp_at_15_rr():
    strat = EmaAdxTrendStrategy()
    series = pullback_then_resume_uptrend()
    sig = strat.evaluate(
        _snapshot(series),
        EmaAdxTrendParams(
            adx_threshold=Decimal("15"),
            atr_stop_mult=Decimal("2"),
            atr_tp_mult=Decimal("3"),
        ),
        _ctx(),
    )
    assert sig is not None
    sl_dist = sig.entry - sig.stop_loss
    tp_dist = sig.take_profits[0] - sig.entry
    rr = tp_dist / sl_dist
    assert rr == Decimal("1.5")
