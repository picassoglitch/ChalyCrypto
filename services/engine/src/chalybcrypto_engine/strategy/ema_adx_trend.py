"""EMA/ADX trend strategy.

MVP set #1 (ARCHITECTURE §3): EMA(35/50) trend + ADX-strength filter, structure-aware.

Rule:
  Long when:
    * EMA(fast) > EMA(slow)
    * ADX(adx_period) >= adx_threshold
    * The current close *crosses up through* EMA(fast) — i.e. prior close was at/below
      EMA(fast) and current close is above. This catches the pullback-completion bar.
  Short when the mirror conditions hold.

Risk shape:
  * Stop  = entry  ∓ atr_stop_mult * ATR
  * TP    = entry  ± atr_tp_mult * ATR     → RR = tp_mult / stop_mult
  * Leverage from params; risk engine clamps to risk profile max.

Pure: no I/O, no LLM, no global state. Same evaluate path runs in backtest/paper/live.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import Field

from chalybcrypto_shared import MarketSnapshot, MarginType, Side, Signal, dedup_hash

from .base import Strategy, StrategyContext, StrategyParams
from .indicators import adx, atr, ema


class EmaAdxTrendParams(StrategyParams):
    fast_period: int = 35
    slow_period: int = 50
    adx_period: int = 14
    adx_threshold: Decimal = Decimal("20")
    atr_period: int = 14
    atr_stop_mult: Decimal = Decimal("2.0")
    atr_tp_mult: Decimal = Decimal("3.0")
    leverage: Decimal = Decimal("10")
    min_bars: int = 200  # ARCHITECTURE §3: "left side of chart" structure validation
    timeframe: str = "5m"


class EmaAdxTrendStrategy(Strategy):
    key = "ema_adx_trend"

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        params: StrategyParams,
        context: StrategyContext,
    ) -> Signal | None:
        if not isinstance(params, EmaAdxTrendParams):
            raise TypeError(f"expected EmaAdxTrendParams, got {type(params).__name__}")

        klines = snapshot.klines
        if len(klines) < params.min_bars:
            return None

        ema_fast = ema(klines, params.fast_period)
        ema_slow = ema(klines, params.slow_period)
        adx_series = adx(klines, params.adx_period)
        atr_series = atr(klines, params.atr_period)

        i = len(klines) - 1
        f_now, f_prev = ema_fast[i], ema_fast[i - 1]
        s_now = ema_slow[i]
        a_now = adx_series[i]
        atr_now = atr_series[i]
        if None in (f_now, f_prev, s_now, a_now, atr_now):
            return None
        if a_now < params.adx_threshold:
            return None

        close_now = klines[i].close
        close_prev = klines[i - 1].close

        # Long: trend up + close crossing up through EMA fast.
        crossed_up = close_prev <= f_prev and close_now > f_now
        crossed_dn = close_prev >= f_prev and close_now < f_now

        if f_now > s_now and crossed_up:
            return self._build(
                snapshot=snapshot,
                params=params,
                context=context,
                side=Side.LONG,
                entry=close_now,
                atr_now=atr_now,
                adx_now=a_now,
            )
        if f_now < s_now and crossed_dn:
            return self._build(
                snapshot=snapshot,
                params=params,
                context=context,
                side=Side.SHORT,
                entry=close_now,
                atr_now=atr_now,
                adx_now=a_now,
            )
        return None

    def _build(
        self,
        *,
        snapshot: MarketSnapshot,
        params: EmaAdxTrendParams,
        context: StrategyContext,
        side: Side,
        entry: Decimal,
        atr_now: Decimal,
        adx_now: Decimal,
    ) -> Signal:
        stop_dist = params.atr_stop_mult * atr_now
        tp_dist = params.atr_tp_mult * atr_now
        if side == Side.LONG:
            sl = entry - stop_dist
            tp = entry + tp_dist
        else:
            sl = entry + stop_dist
            tp = entry - tp_dist
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
                "ema_aligned",
                f"adx_{int(adx_now)}",
                "cross_up" if side == Side.LONG else "cross_down",
            ],
            source="scanner",
            dedup_hash=h,
            created_at=context.now,
        )
