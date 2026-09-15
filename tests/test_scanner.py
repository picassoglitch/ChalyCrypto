"""Scanner tests — mocked Bitunix in, structured ScanResult out."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from chalybcrypto_engine.strategy import EmaAdxTrendParams, EmaAdxTrendStrategy
from chalybcrypto_worker import scan_once
from chalybcrypto_shared import Kline

from tests.strategy._helpers import flat_series, pullback_then_resume_uptrend


NOW = datetime(2026, 6, 6, 16, 0, tzinfo=timezone.utc)


@dataclass
class _FakeFunding:
    mark_price: Decimal = Decimal("60000")
    funding_rate: Decimal = Decimal("0.0001")


class _FakeSource:
    """KlineSource impl that hands back a fixed series + fixed funding."""

    def __init__(self, series: list[Kline]) -> None:
        self._series = series

    async def klines(self, pair: str, interval: str, *, limit: int = 100) -> list[Kline]:
        return self._series[-limit:]

    async def funding(self, pair: str):
        return _FakeFunding(
            mark_price=self._series[-1].close,
            funding_rate=Decimal("0.0001"),
        )


async def test_scan_once_returns_no_signal_on_flat_market():
    src = _FakeSource(flat_series(220, price=100))
    result = await scan_once(src, "BTCUSDT", interval="5m", bars=220, now=NOW)
    assert result.kline_count == 220
    assert result.mark_price == Decimal("100")
    assert result.pair == "BTCUSDT"
    assert len(result.outcomes) == 3  # MVP three
    for o in result.outcomes:
        assert o.signal is None
        assert o.decision is None


async def test_scan_once_fires_ema_adx_trend_on_constructed_setup():
    series = pullback_then_resume_uptrend()  # last bar is the cross-up
    src = _FakeSource(series)
    result = await scan_once(src, "BTCUSDT", interval="5m", bars=len(series), now=NOW)

    by_key = {o.strategy_key: o for o in result.outcomes}
    ema = by_key["ema_adx_trend"]
    assert ema.fired
    assert ema.signal.side.value == "long"
    # In paper mode with no stats, EV gate allows through; risk engine still applies sizing/liq.
    assert ema.decision is not None


async def test_scan_once_uses_only_supplied_strategies():
    series = pullback_then_resume_uptrend()
    src = _FakeSource(series)
    result = await scan_once(
        src, "BTCUSDT", interval="5m", bars=len(series),
        strategies=[(EmaAdxTrendStrategy(), EmaAdxTrendParams(adx_threshold=Decimal("18")))],
        now=NOW,
    )
    assert len(result.outcomes) == 1
    assert result.outcomes[0].strategy_key == "ema_adx_trend"


async def test_scan_once_preserves_pair_and_funding_in_result():
    src = _FakeSource(flat_series(220, price=100))
    result = await scan_once(src, "ETHUSDT", interval="5m", bars=220, now=NOW)
    assert result.pair == "ETHUSDT"
    assert result.funding_rate == Decimal("0.0001")
