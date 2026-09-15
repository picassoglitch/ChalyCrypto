"""VWAP / RSI mean-reversion strategy.

MVP set #2 (ARCHITECTURE §3): mean-reversion is regime-gated to LOW-ADX (sideways).
Trying to mean-revert during a trend is the canonical way to bleed money on this lane.

Rule:
  Long when:
    * ADX(14) < adx_ceiling                      (no strong trend)
    * close < VWAP * (1 - deviation_pct)          (extended below)
    * RSI(14) < rsi_oversold                       (oversold)
  Short is the mirror with > VWAP*(1+dev) and RSI > overbought.

Target: revert to VWAP. SL: ATR-based, tighter than trend.
"""

from __future__ import annotations

from decimal import Decimal

from chalybcrypto_shared import MarginType, MarketSnapshot, Side, Signal, dedup_hash

from .base import Strategy, StrategyContext, StrategyParams
from .indicators import adx, atr, rsi, vwap


class VwapRsiMeanRevParams(StrategyParams):
    adx_period: int = 14
    adx_ceiling: Decimal = Decimal("20")  # only fire when trend strength is low
    rsi_period: int = 14
    rsi_oversold: Decimal = Decimal("30")
    rsi_overbought: Decimal = Decimal("70")
    vwap_deviation_pct: Decimal = Decimal("0.005")  # 50bp extended from VWAP
    atr_period: int = 14
    atr_stop_mult: Decimal = Decimal("1.5")  # tighter than trend
    leverage: Decimal = Decimal("5")  # lower leverage for mean-rev
    min_bars: int = 200
    timeframe: str = "5m"


class VwapRsiMeanRevStrategy(Strategy):
    key = "vwap_rsi_meanrev"

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        params: StrategyParams,
        context: StrategyContext,
    ) -> Signal | None:
        if not isinstance(params, VwapRsiMeanRevParams):
            raise TypeError(f"expected VwapRsiMeanRevParams, got {type(params).__name__}")

        klines = snapshot.klines
        if len(klines) < params.min_bars:
            return None

        i = len(klines) - 1
        a = adx(klines, params.adx_period)[i]
        r = rsi(klines, params.rsi_period)[i]
        v = vwap(klines)[i]
        atr_now = atr(klines, params.atr_period)[i]
        if None in (a, r, v, atr_now):
            return None

        # Regime gate: only mean-revert in low-ADX (sideways) markets.
        if a >= params.adx_ceiling:
            return None

        close = klines[i].close
        upper_band = v * (Decimal("1") + params.vwap_deviation_pct)
        lower_band = v * (Decimal("1") - params.vwap_deviation_pct)

        if close < lower_band and r < params.rsi_oversold:
            sl = close - params.atr_stop_mult * atr_now
            tp = v  # target is reversion to VWAP itself
            return self._build(snapshot, params, context, Side.LONG, close, sl, tp, a, r, v)

        if close > upper_band and r > params.rsi_overbought:
            sl = close + params.atr_stop_mult * atr_now
            tp = v
            return self._build(snapshot, params, context, Side.SHORT, close, sl, tp, a, r, v)

        return None

    def _build(
        self,
        snapshot: MarketSnapshot,
        params: VwapRsiMeanRevParams,
        context: StrategyContext,
        side: Side,
        entry: Decimal,
        sl: Decimal,
        tp: Decimal,
        adx_now: Decimal,
        rsi_now: Decimal,
        vwap_now: Decimal,
    ) -> Signal:
        h = dedup_hash(self.key, snapshot.pair, side.value, entry, context.now.isoformat())
        return Signal(
            pair=snapshot.pair,
            side=side,
            strategy=self.key,
            entry=entry,
            stop_loss=sl,
            take_profits=[tp],
            leverage=params.leverage,
            margin_type=MarginType.ISOLATED,
            timeframe=params.timeframe,
            thesis_tags=[
                "mean_reversion",
                f"adx_{int(adx_now)}_low_regime",
                f"rsi_{int(rsi_now)}",
                "above_vwap" if side == Side.SHORT else "below_vwap",
            ],
            source="scanner",
            dedup_hash=h,
            created_at=context.now,
        )
