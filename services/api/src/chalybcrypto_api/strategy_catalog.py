"""The engine's strategies with the parameters the scanner runs them with
(services/worker scanner.default_strategies, ARCHITECTURE §3). One place for
the backtest runner and the Señales feed, so both see what paper/live sees."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from chalybcrypto_engine.strategy import (
    EmaAdxTrendParams,
    EmaAdxTrendStrategy,
    FvgObParams,
    FvgObStrategy,
    VwapRsiMeanRevParams,
    VwapRsiMeanRevStrategy,
)
from chalybcrypto_engine.strategy.base import Strategy, StrategyParams

CATALOG: dict[str, tuple[Callable[[], Strategy], StrategyParams]] = {
    "ema_adx_trend": (EmaAdxTrendStrategy, EmaAdxTrendParams(adx_threshold=Decimal("18"))),
    "vwap_rsi_meanrev": (VwapRsiMeanRevStrategy, VwapRsiMeanRevParams(adx_ceiling=Decimal("25"))),
    "fvg_ob": (
        FvgObStrategy,
        FvgObParams(min_gap_bps=Decimal("8"), min_impulse_bps=Decimal("25")),
    ),
}

# Bars of history each evaluate() sees: the live scanner's window.
LOOKBACK_BARS = 300


def build(key: str) -> tuple[Strategy, StrategyParams]:
    make, params = CATALOG[key]
    return make(), params
