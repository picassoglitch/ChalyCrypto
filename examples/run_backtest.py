"""End-to-end backtest example.

  python examples/run_backtest.py

Pulls public Binance USDⓈ-M klines for BTCUSDT (no keys), runs the EMA/ADX trend
strategy, and prints a metrics summary. CLAUDE.md §0.3: Binance is permitted only as a
data-only backtest source; execution stays on Bitunix/LBank.

Tweak SYMBOL, INTERVAL, BARS, RISK_BPS at the top of the file.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from chalybcrypto_connectors import BinanceDataConnector
from chalybcrypto_engine.backtest import Backtester, ConservativeFillModel
from chalybcrypto_engine.strategy import EmaAdxTrendParams, EmaAdxTrendStrategy
from chalybcrypto_shared import FeeSchedule
from datetime import datetime, timezone


SYMBOL = "BTCUSDT"
INTERVAL = "5m"
BARS = 1500
RISK_BPS = Decimal("50")  # 0.5% risk per trade


async def main() -> None:
    binance = BinanceDataConnector()
    try:
        klines = await binance.klines(SYMBOL, INTERVAL, limit=BARS)
    finally:
        await binance.aclose()

    print(f"fetched {len(klines)} bars: "
          f"{klines[0].open_time.isoformat()} -> {klines[-1].close_time.isoformat()}")

    fee = FeeSchedule(
        exchange="bitunix", symbol=None, vip_level="VIP0",
        maker_bps=Decimal("2"), taker_bps=Decimal("6"),
        effective_at=datetime.now(timezone.utc), source="seed",
    )
    fill_model = ConservativeFillModel(
        fee_schedule=fee,
        spread_bps=Decimal("1"),
        slippage_bps=Decimal("1"),
        funding_rate_per_interval=Decimal("0.0001"),
        funding_interval_hours=8,
    )

    bt = Backtester(
        EmaAdxTrendStrategy(),
        fill_model,
        starting_equity=Decimal("10000"),
        risk_per_trade_bps=RISK_BPS,
    )
    report = bt.run(
        klines,
        EmaAdxTrendParams(adx_threshold=Decimal("18")),
        pair=SYMBOL,
        timeframe=INTERVAL,
    )

    m = report.metrics
    print()
    print(f"  strategy:        {report.strategy_key}")
    print(f"  pair / tf:       {report.pair} / {report.timeframe}")
    print(f"  starting equity: {report.starting_equity}")
    print(f"  ending equity:   {report.ending_equity}")
    print(f"  trades:          {m.sample_size}")
    print(f"  win rate:        {m.win_rate}")
    print(f"  profit factor:   {m.profit_factor}")
    print(f"  avg RR:          {m.avg_rr}")
    print(f"  avg win bps:     {m.avg_win_bps}")
    print(f"  avg loss bps:    {m.avg_loss_bps}")
    print(f"  max drawdown:    {m.max_drawdown_bps} bps")
    print(f"  fee drag:        {m.fee_drag_bps} bps")
    print(f"  optimistic:      {report.optimistic}  <-- CLAUDE.md rule")


if __name__ == "__main__":
    asyncio.run(main())
