"""Synthetic kline builders for strategy + backtest tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from chalybcrypto_shared import Kline


BAR_MINUTES = 5


def _bar(*, t: datetime, o, h, l, c, v="1") -> Kline:
    return Kline(
        open_time=t,
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(l)),
        close=Decimal(str(c)),
        volume=Decimal(str(v)),
        close_time=t + timedelta(minutes=BAR_MINUTES),
    )


def flat_series(n: int, *, price: float = 100.0, start: datetime | None = None) -> list[Kline]:
    """Boring sideways data — no strategy should fire on this."""
    t0 = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [
        _bar(
            t=t0 + timedelta(minutes=i * BAR_MINUTES),
            o=price, h=price + 0.05, l=price - 0.05, c=price, v=10,
        )
        for i in range(n)
    ]


def linear_trend(n: int, *, start_price: float = 100.0, step: float = 0.05,
                 start: datetime | None = None) -> list[Kline]:
    """Clean upward-stepping series. Crosses up through EMAs after seeding period."""
    t0 = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
    out: list[Kline] = []
    px = start_price
    for i in range(n):
        nx = px + step
        out.append(
            _bar(
                t=t0 + timedelta(minutes=i * BAR_MINUTES),
                o=px, h=nx + 0.02, l=px - 0.02, c=nx, v=10,
            )
        )
        px = nx
    return out


def downtrend(n: int, *, start_price: float = 200.0, step: float = 0.05,
              start: datetime | None = None) -> list[Kline]:
    return linear_trend(n, start_price=start_price, step=-step, start=start)


def pullback_then_resume_uptrend(
    n_pre: int = 200, n_pull: int = 5, n_rip: int = 1,
    base: float = 100.0, trend_step: float = 0.15,
    pull_step: float = 3.0, rip_step: float = 13.0,
) -> list[Kline]:
    """Build a series with: long warmup uptrend, deep pullback that pushes close *below*
    EMA(35), then a single ripping bar that closes back above EMA(35).

    The last bar should be the cross-up trigger for a LONG signal. Defaults are tuned so
    the final close > EMA(35) > pre-final close, which is the cross condition.
    """
    pre = linear_trend(n_pre, start_price=base, step=trend_step)
    last = float(pre[-1].close)
    t_next = pre[-1].close_time
    pull: list[Kline] = []
    for _ in range(n_pull):
        nx = last - pull_step
        pull.append(_bar(t=t_next, o=last, h=last + 0.05, l=nx - 0.05, c=nx, v=10))
        last = nx
        t_next = t_next + timedelta(minutes=BAR_MINUTES)
    rip: list[Kline] = []
    for _ in range(n_rip):
        nx = last + rip_step
        rip.append(_bar(t=t_next, o=last, h=nx + 0.05, l=last - 0.05, c=nx, v=10))
        last = nx
        t_next = t_next + timedelta(minutes=BAR_MINUTES)
    return pre + pull + rip
