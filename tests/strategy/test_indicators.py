from __future__ import annotations

from decimal import Decimal

from chalybcrypto_engine.strategy.indicators import (
    adx,
    atr,
    ema,
    macd,
    rsi,
    true_range,
    vwap,
)

from ._helpers import flat_series, linear_trend


def test_ema_warmup_then_tracks_signal():
    series = linear_trend(50, start_price=100, step=1.0)
    out = ema(series, period=10)
    assert all(v is None for v in out[:9])
    assert out[9] is not None
    # On a rising series with step 1.0, EMA(10) value should always lag close by < step*period.
    for i in range(9, len(series)):
        assert out[i] is not None and out[i] <= series[i].close


def test_ema_constant_series_equals_constant():
    series = flat_series(30, price=100)
    out = ema(series, period=5)
    for v in out[4:]:
        assert v == Decimal("100")


def test_rsi_constant_series_is_50_or_below():
    """No movement means avg_gain=avg_loss=0 → divide-by-zero branch returns 50."""
    series = flat_series(30, price=100)
    out = rsi(series, period=14)
    assert out[14] is not None
    assert out[14] == Decimal("50")


def test_rsi_uptrend_pushes_above_70():
    series = linear_trend(50, start_price=100, step=1.0)
    out = rsi(series, period=14)
    # Pure uptrend with no losses → RSI saturates near 100.
    assert out[-1] is not None and out[-1] > Decimal("99")


def test_true_range_first_bar_uses_simple_high_minus_low():
    series = linear_trend(5, start_price=100, step=1.0)
    tr = true_range(series)
    # Bar 0 of linear_trend(step=1.0): o=100, h=101.02, l=99.98 → h-l = 1.04
    assert tr[0] == Decimal("1.04")


def test_atr_warmup_and_positivity():
    series = linear_trend(50, start_price=100, step=1.0)
    out = atr(series, period=14)
    assert all(v is None for v in out[:13])
    assert out[13] is not None and out[13] > 0


def test_adx_warmup_returns_none_until_double_period_minus_one():
    series = linear_trend(60, start_price=100, step=1.0)
    out = adx(series, period=14)
    # ADX needs 2*period - 1 warmup approximately.
    assert all(v is None for v in out[:14])
    assert any(v is not None for v in out[27:])


def test_adx_higher_on_trend_than_flat():
    trend = linear_trend(80, start_price=100, step=1.0)
    flat = flat_series(80, price=100)
    a_trend = adx(trend, period=14)
    a_flat = adx(flat, period=14)
    # Take last non-None of each
    last_trend = [v for v in a_trend if v is not None][-1]
    last_flat = [v for v in a_flat if v is not None][-1] if any(v is not None for v in a_flat) else Decimal("0")
    assert last_trend > last_flat


def test_macd_constant_series_is_zero_after_warmup():
    """No trend → fast EMA == slow EMA → MACD line is 0, signal is 0, hist is 0."""
    series = flat_series(60, price=100)
    line, sig, hist = macd(series, fast=12, slow=26, signal=9)
    # First non-None values appear at index slow-1 for line, slow-1+signal-1 for signal.
    assert line[25] == Decimal(0)
    assert sig[33] == Decimal(0)
    assert hist[33] == Decimal(0)


def test_macd_uptrend_line_positive_and_hist_nonnegative():
    """Rising series: fast EMA tracks closer to price than slow → MACD line > 0.
    On a smooth uptrend the histogram never goes negative. (On a perfectly linear
    trend the line stabilises and signal converges to it; on any acceleration the
    line leads and hist > 0.)"""
    series = linear_trend(80, start_price=100, step=1.0)
    line, sig, hist = macd(series, fast=12, slow=26, signal=9)
    last_line = [v for v in line if v is not None][-1]
    # MACD line stabilises near step * (slow - fast)/2 = 7.0 in a constant-step trend.
    assert last_line > Decimal(6) and last_line < Decimal(8)
    # Histogram never goes negative on a pure uptrend.
    assert all(v >= Decimal(0) for v in hist if v is not None)


def test_macd_warmup_lengths_match_definition():
    series = linear_trend(60, start_price=100, step=1.0)
    line, sig, hist = macd(series, fast=12, slow=26, signal=9)
    # MACD line needs `slow` warmup → index 0..24 None, 25 first value.
    assert all(v is None for v in line[:25])
    assert line[25] is not None
    # Signal needs `slow + signal - 1` warmup → index 0..32 None, 33 first value.
    assert all(v is None for v in sig[:33])
    assert sig[33] is not None
    # Hist mirrors signal alignment.
    assert all(v is None for v in hist[:33])
    assert hist[33] is not None


def test_macd_rejects_fast_ge_slow():
    series = flat_series(30, price=100)
    try:
        macd(series, fast=26, slow=12, signal=9)
    except ValueError:
        return
    raise AssertionError("expected ValueError for fast >= slow")


def test_vwap_aggregates_typical_price_by_volume():
    series = linear_trend(10, start_price=100, step=1.0)
    v = vwap(series)
    assert v[-1] is not None
    # Sanity: VWAP lies between first and last typical-price
    first_t = (series[0].high + series[0].low + series[0].close) / Decimal("3")
    last_t = (series[-1].high + series[-1].low + series[-1].close) / Decimal("3")
    assert first_t < v[-1] < last_t
