"""FVG + Order Block structure strategy.

MVP set #3 (ARCHITECTURE §3): "ICT/structure: FVG + Order Block + liquidity sweep".
Based on the user's `FVGs y Order Blocks` source doc.

A **bullish Fair Value Gap** is a 3-bar pattern where bar1.high < bar3.low — there is a
price range that bar2 jumped over without trading, indicating institutional imbalance.
Theory: price often returns to "fill the gap" before continuing the impulse.

This strategy:
  1. Looks back over the recent `lookback` bars for the *most recent* bullish (or bearish) FVG.
  2. Fires LONG when the current close re-enters the gap from above (filling it) AND the
     bar that *opened* the gap was preceded by an impulse move (close[bar1] < close[bar3]
     with a configurable minimum range).
  3. SL = below the FVG low (long); TP = the swing high of the impulse leg.

Pure function. Same evaluate-path runs in backtest/paper/live.
"""

from __future__ import annotations

from decimal import Decimal

from chalybcrypto_shared import MarginType, MarketSnapshot, Side, Signal, dedup_hash

from .base import Strategy, StrategyContext, StrategyParams


class FvgObParams(StrategyParams):
    lookback: int = 40
    min_gap_bps: Decimal = Decimal("10")  # 10 bp minimum gap to count as meaningful
    min_impulse_bps: Decimal = Decimal("30")  # close move from bar1 to bar3
    leverage: Decimal = Decimal("8")
    min_bars: int = 60
    timeframe: str = "5m"


class FvgObStrategy(Strategy):
    key = "fvg_ob"

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        params: StrategyParams,
        context: StrategyContext,
    ) -> Signal | None:
        if not isinstance(params, FvgObParams):
            raise TypeError(f"expected FvgObParams, got {type(params).__name__}")

        klines = snapshot.klines
        if len(klines) < params.min_bars:
            return None

        last = klines[-1]
        last_close = last.close
        # Scan from oldest-to-newest within the lookback window for the most recent FVG.
        start = max(0, len(klines) - params.lookback - 3)
        most_recent_bullish: tuple[int, Decimal, Decimal] | None = None  # (idx_of_bar3, gap_low, gap_high)
        most_recent_bearish: tuple[int, Decimal, Decimal] | None = None
        for j in range(start, len(klines) - 2):
            b1, b2, b3 = klines[j], klines[j + 1], klines[j + 2]
            # Bullish FVG: b1.high < b3.low. Gap spans (b1.high, b3.low).
            if b1.high < b3.low and b3.close > b1.close:
                gap_size_bps = (b3.low - b1.high) / b1.high * Decimal("10000")
                impulse_bps = (b3.close - b1.close) / b1.close * Decimal("10000")
                if gap_size_bps >= params.min_gap_bps and impulse_bps >= params.min_impulse_bps:
                    most_recent_bullish = (j + 2, b1.high, b3.low)
            # Bearish FVG: b1.low > b3.high. Gap spans (b3.high, b1.low).
            if b1.low > b3.high and b3.close < b1.close:
                gap_size_bps = (b1.low - b3.high) / b3.high * Decimal("10000")
                impulse_bps = (b1.close - b3.close) / b1.close * Decimal("10000")
                if gap_size_bps >= params.min_gap_bps and impulse_bps >= params.min_impulse_bps:
                    most_recent_bearish = (j + 2, b3.high, b1.low)

        # Bullish setup: price has fallen back into the bullish gap from above; current
        # bar's low touched or entered the gap zone.
        if most_recent_bullish is not None:
            idx3, gap_low, gap_high = most_recent_bullish
            if last.low <= gap_high and last_close > gap_low:
                # Use the impulse leg's swing high as TP.
                impulse_high = max((k.high for k in klines[idx3 - 2 : idx3 + 1]), default=last.high)
                # SL just below the gap low.
                sl = gap_low * (Decimal("1") - Decimal("0.001"))  # 10 bp buffer
                tp = impulse_high
                if tp > last_close > sl:
                    return self._build(
                        snapshot, params, context, Side.LONG, last_close, sl, tp,
                        ["bullish_fvg", "gap_filled"],
                    )

        if most_recent_bearish is not None:
            idx3, gap_low, gap_high = most_recent_bearish
            if last.high >= gap_low and last_close < gap_high:
                impulse_low = min((k.low for k in klines[idx3 - 2 : idx3 + 1]), default=last.low)
                sl = gap_high * (Decimal("1") + Decimal("0.001"))
                tp = impulse_low
                if sl > last_close > tp:
                    return self._build(
                        snapshot, params, context, Side.SHORT, last_close, sl, tp,
                        ["bearish_fvg", "gap_filled"],
                    )

        return None

    def _build(
        self,
        snapshot: MarketSnapshot,
        params: FvgObParams,
        context: StrategyContext,
        side: Side,
        entry: Decimal,
        sl: Decimal,
        tp: Decimal,
        tags: list[str],
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
            thesis_tags=tags,
            source="scanner",
            dedup_hash=h,
            created_at=context.now,
        )
