from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from chalybcrypto_engine.strategy import FvgObParams, FvgObStrategy, StrategyContext
from chalybcrypto_shared import Kline, MarketSnapshot, Side

from ._helpers import flat_series, _bar, BAR_MINUTES


def _snapshot(klines):
    return MarketSnapshot(
        pair="BTCUSDT", exchange="binance", taken_at=klines[-1].close_time,
        klines=klines, mark_price=klines[-1].close,
    )


def _ctx():
    return StrategyContext(now=datetime(2026, 6, 6, tzinfo=timezone.utc))


def _build_bullish_fvg_then_fill() -> list[Kline]:
    """200 flat bars at 100, then a 3-bar bullish FVG, then drift back into the gap."""
    bars = flat_series(200, price=100)
    last_t = bars[-1].close_time
    # bar1: range 99.5 - 100.0
    bars.append(_bar(t=last_t, o=100, h=100.0, l=99.5, c=99.7, v=10))
    last_t = last_t + timedelta(minutes=BAR_MINUTES)
    # bar2: impulse, jumps over the gap (its low > bar1.high, far above)
    bars.append(_bar(t=last_t, o=100.2, h=102.5, l=101.0, c=102.4, v=20))
    last_t = last_t + timedelta(minutes=BAR_MINUTES)
    # bar3: continues impulse, low at 101.5 (so gap is bar1.high=100.0 → bar3.low=101.5,
    # 150bps gap), close at 103.0
    bars.append(_bar(t=last_t, o=102.4, h=103.5, l=101.5, c=103.0, v=15))
    last_t = last_t + timedelta(minutes=BAR_MINUTES)
    # bar4: drift down toward the gap zone but not into it yet
    bars.append(_bar(t=last_t, o=103.0, h=103.1, l=102.5, c=102.8, v=10))
    last_t = last_t + timedelta(minutes=BAR_MINUTES)
    # bar5: dips into the gap — low touches 100.8 (inside gap 100.0..101.5), closes 101.2
    bars.append(_bar(t=last_t, o=102.8, h=102.9, l=100.8, c=101.2, v=12))
    return bars


def test_short_series_returns_none():
    strat = FvgObStrategy()
    sig = strat.evaluate(
        _snapshot(flat_series(30, price=100)), FvgObParams(), _ctx()
    )
    assert sig is None  # below min_bars


def test_no_signal_on_flat_series():
    strat = FvgObStrategy()
    sig = strat.evaluate(
        _snapshot(flat_series(220, price=100)), FvgObParams(), _ctx()
    )
    assert sig is None  # no gaps in flat data


def test_bullish_fvg_fill_fires_long():
    strat = FvgObStrategy()
    klines = _build_bullish_fvg_then_fill()
    sig = strat.evaluate(_snapshot(klines), FvgObParams(min_gap_bps=Decimal("10")), _ctx())
    assert sig is not None
    assert sig.side == Side.LONG
    assert sig.entry == klines[-1].close
    assert sig.stop_loss < sig.entry  # below gap low
    assert sig.take_profits[0] > sig.entry  # at impulse high
    assert "bullish_fvg" in sig.thesis_tags
