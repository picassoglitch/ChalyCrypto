from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from chalybcrypto_engine.strategy import (
    StrategyContext,
    VwapRsiMeanRevParams,
    VwapRsiMeanRevStrategy,
)
from chalybcrypto_shared import Kline, MarketSnapshot, Side

from ._helpers import flat_series, linear_trend, _bar, BAR_MINUTES


def _snapshot(klines):
    return MarketSnapshot(
        pair="BTCUSDT", exchange="binance", taken_at=klines[-1].close_time,
        klines=klines, mark_price=klines[-1].close,
    )


def _ctx():
    return StrategyContext(now=datetime(2026, 6, 6, tzinfo=timezone.utc))


def test_no_signal_on_trend_high_adx_regime():
    strat = VwapRsiMeanRevStrategy()
    # Strong uptrend → ADX high → mean-rev should NOT fire even if other conditions met.
    series = linear_trend(220, start_price=100, step=1.0)
    sig = strat.evaluate(_snapshot(series), VwapRsiMeanRevParams(), _ctx())
    assert sig is None


def _ranging_with_dip_then_oversold(n_pre: int = 200, n_dip: int = 6) -> list[Kline]:
    """Sideways with a sudden dip → low ADX + RSI oversold + below VWAP."""
    base = flat_series(n_pre, price=100)
    last_t = base[-1].close_time
    px = 100.0
    dip: list[Kline] = []
    for _ in range(n_dip):
        nx = px - 0.5
        dip.append(_bar(t=last_t, o=px, h=px + 0.05, l=nx - 0.05, c=nx, v=10))
        px = nx
        last_t = last_t + timedelta(minutes=BAR_MINUTES)
    return base + dip


def test_fires_long_on_oversold_below_vwap_in_low_adx_regime():
    strat = VwapRsiMeanRevStrategy()
    series = _ranging_with_dip_then_oversold()
    sig = strat.evaluate(
        _snapshot(series),
        VwapRsiMeanRevParams(
            adx_ceiling=Decimal("60"),  # permissive
            rsi_oversold=Decimal("40"),
            vwap_deviation_pct=Decimal("0.001"),  # 10 bps deviation
            atr_stop_mult=Decimal("1.5"),
        ),
        _ctx(),
    )
    assert sig is not None
    assert sig.side == Side.LONG
    assert sig.stop_loss < sig.entry
    assert sig.take_profits[0] > sig.entry  # TP at VWAP, which is above current price
    assert "mean_reversion" in sig.thesis_tags


def test_no_signal_when_adx_above_ceiling():
    """Setup is right but ADX gate blocks it."""
    strat = VwapRsiMeanRevStrategy()
    series = _ranging_with_dip_then_oversold()
    sig = strat.evaluate(
        _snapshot(series),
        VwapRsiMeanRevParams(adx_ceiling=Decimal("5")),  # ridiculously low ceiling
        _ctx(),
    )
    assert sig is None
