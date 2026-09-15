"""Indicator library — pure functions over Kline lists.

All math uses Decimal. Each indicator returns a list aligned to the input length, with
None during the warmup window. This makes strategies trivially index-by-bar without
length-tracking. ARCHITECTURE §0.5: each strategy uses a *small set of orthogonal*
filters — these are the building blocks, not a per-trade soup.
"""

from __future__ import annotations

from decimal import Decimal

from chalybcrypto_shared import Kline


def ema(klines: list[Kline], period: int) -> list[Decimal | None]:
    """Exponential moving average of close, alpha = 2/(n+1).

    Seeds the first value at index `period-1` with a simple mean of the first `period`
    closes, then folds new closes with alpha. Indices 0..period-2 are None.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if len(klines) < period:
        return [None] * len(klines)
    alpha = Decimal(2) / Decimal(period + 1)
    out: list[Decimal | None] = [None] * (period - 1)
    seed = sum((k.close for k in klines[:period]), start=Decimal(0)) / Decimal(period)
    out.append(seed)
    prev = seed
    for k in klines[period:]:
        cur = alpha * k.close + (Decimal(1) - alpha) * prev
        out.append(cur)
        prev = cur
    return out


def _wilder_smooth(values: list[Decimal], period: int) -> list[Decimal | None]:
    """Wilder's smoothing: seed = sum of first `period`, then S_t = S_{t-1} - S_{t-1}/n + x_t."""
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        return [None] * len(values)
    out: list[Decimal | None] = [None] * (period - 1)
    seed = sum(values[:period], start=Decimal(0))
    out.append(seed)
    prev = seed
    n = Decimal(period)
    for v in values[period:]:
        cur = prev - (prev / n) + v
        out.append(cur)
        prev = cur
    return out


def rsi(klines: list[Kline], period: int = 14) -> list[Decimal | None]:
    """Wilder's RSI on close. First `period` values are None; the value at `period` uses
    the simple-average seed, then Wilder's smoothing thereafter."""
    if len(klines) <= period:
        return [None] * len(klines)
    gains: list[Decimal] = [Decimal(0)]
    losses: list[Decimal] = [Decimal(0)]
    for i in range(1, len(klines)):
        diff = klines[i].close - klines[i - 1].close
        gains.append(diff if diff > 0 else Decimal(0))
        losses.append(-diff if diff < 0 else Decimal(0))

    out: list[Decimal | None] = [None] * period
    avg_gain = sum(gains[1 : period + 1], start=Decimal(0)) / Decimal(period)
    avg_loss = sum(losses[1 : period + 1], start=Decimal(0)) / Decimal(period)
    out.append(_rsi_from_gl(avg_gain, avg_loss))
    n = Decimal(period)
    for i in range(period + 1, len(klines)):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n
        out.append(_rsi_from_gl(avg_gain, avg_loss))
    return out


def _rsi_from_gl(g: Decimal, l: Decimal) -> Decimal:
    if l == 0:
        return Decimal(100) if g > 0 else Decimal(50)
    rs = g / l
    return Decimal(100) - (Decimal(100) / (Decimal(1) + rs))


def true_range(klines: list[Kline]) -> list[Decimal]:
    out: list[Decimal] = [klines[0].high - klines[0].low]
    for i in range(1, len(klines)):
        h, l = klines[i].high, klines[i].low
        pc = klines[i - 1].close
        out.append(max(h - l, abs(h - pc), abs(l - pc)))
    return out


def atr(klines: list[Kline], period: int = 14) -> list[Decimal | None]:
    """Wilder ATR. Seed = simple mean of first `period` true ranges, then Wilder smoothing."""
    if len(klines) < period:
        return [None] * len(klines)
    tr = true_range(klines)
    out: list[Decimal | None] = [None] * (period - 1)
    seed = sum(tr[:period], start=Decimal(0)) / Decimal(period)
    out.append(seed)
    prev = seed
    n = Decimal(period)
    for i in range(period, len(klines)):
        cur = (prev * (n - 1) + tr[i]) / n
        out.append(cur)
        prev = cur
    return out


def adx(klines: list[Kline], period: int = 14) -> list[Decimal | None]:
    """Wilder ADX(period). Returns aligned list; warmup region is None."""
    if len(klines) <= period:
        return [None] * len(klines)

    tr = true_range(klines)
    plus_dm: list[Decimal] = [Decimal(0)]
    minus_dm: list[Decimal] = [Decimal(0)]
    for i in range(1, len(klines)):
        up = klines[i].high - klines[i - 1].high
        down = klines[i - 1].low - klines[i].low
        plus_dm.append(up if up > 0 and up > down else Decimal(0))
        minus_dm.append(down if down > 0 and down > up else Decimal(0))

    tr_s = _wilder_smooth(tr, period)
    plus_s = _wilder_smooth(plus_dm, period)
    minus_s = _wilder_smooth(minus_dm, period)

    out: list[Decimal | None] = [None] * len(klines)
    dx_series: list[Decimal] = []
    for i in range(len(klines)):
        ts = tr_s[i]
        ps = plus_s[i]
        ms = minus_s[i]
        if ts is None or ps is None or ms is None or ts == 0:
            continue
        plus_di = Decimal(100) * ps / ts
        minus_di = Decimal(100) * ms / ts
        denom = plus_di + minus_di
        if denom == 0:
            dx_series.append(Decimal(0))
        else:
            dx_series.append(Decimal(100) * abs(plus_di - minus_di) / denom)

    # ADX = Wilder smoothed DX. Needs another `period` warmup before first value.
    if len(dx_series) < period:
        return out
    n = Decimal(period)
    seed_adx = sum(dx_series[:period], start=Decimal(0)) / Decimal(period)
    # first ADX value sits at index = period-1 (in DX series) + (period-1) offset in price series
    first_idx = (period - 1) + (period - 1) + 1  # +1 because Wilder smoothing starts indexing from period-1
    if first_idx >= len(klines):
        return out
    out[first_idx] = seed_adx
    prev = seed_adx
    for j, dx in enumerate(dx_series[period:], start=first_idx + 1):
        if j >= len(out):
            break
        cur = (prev * (n - 1) + dx) / n
        out[j] = cur
        prev = cur
    return out


def macd(
    klines: list[Kline],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[list[Decimal | None], list[Decimal | None], list[Decimal | None]]:
    """Standard MACD: (macd_line, signal_line, histogram) all aligned to klines.

    macd_line   = EMA(close, fast) - EMA(close, slow)
    signal_line = EMA(macd_line, signal)   # computed on the non-None tail
    histogram   = macd_line - signal_line
    """
    if fast <= 0 or slow <= 0 or signal <= 0:
        raise ValueError("periods must be positive")
    if fast >= slow:
        raise ValueError("fast period must be less than slow")
    n = len(klines)
    ema_fast = ema(klines, fast)
    ema_slow = ema(klines, slow)
    macd_line: list[Decimal | None] = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(ema_fast, ema_slow)
    ]

    first = next((i for i, v in enumerate(macd_line) if v is not None), None)
    signal_line: list[Decimal | None] = [None] * n
    if first is not None and (n - first) >= signal:
        alpha = Decimal(2) / Decimal(signal + 1)
        tail = macd_line[first:]
        seed = sum(tail[:signal], start=Decimal(0)) / Decimal(signal)
        signal_line[first + signal - 1] = seed
        prev = seed
        for j, v in enumerate(tail[signal:], start=first + signal):
            cur = alpha * v + (Decimal(1) - alpha) * prev
            signal_line[j] = cur
            prev = cur

    hist: list[Decimal | None] = [
        (m - s) if (m is not None and s is not None) else None
        for m, s in zip(macd_line, signal_line)
    ]
    return macd_line, signal_line, hist


def vwap(klines: list[Kline]) -> list[Decimal | None]:
    """Cumulative VWAP from the start of the series. (Per-session VWAP is a wrapper.)"""
    out: list[Decimal | None] = []
    cum_pv = Decimal(0)
    cum_v = Decimal(0)
    for k in klines:
        typical = (k.high + k.low + k.close) / Decimal(3)
        cum_pv += typical * k.volume
        cum_v += k.volume
        out.append(cum_pv / cum_v if cum_v > 0 else None)
    return out
