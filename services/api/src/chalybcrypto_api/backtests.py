"""Backtest runner behind POST /api/backtests (and /api/hub/v1/backtests).

Runs in the request (the caller waits, so Cloud Run keeps CPU for it), on
historical klines (history.py), with the engine's Backtester and the
conservative fill model. Every result is labelled OPTIMISTIC (CLAUDE.md). The
route wraps this in the hub's usage admission: settle "succeeded" only when a
result comes back, "failed" otherwise (consumption contract).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from chalybcrypto_engine.backtest import Backtester, ConservativeFillModel
from chalybcrypto_shared import FeeSchedule, get_settings

from .history import fetch_history
from .strategy_catalog import LOOKBACK_BARS, build

BACKTEST_INTERVALS = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")
_BAR_HOURS = {
    "1m": Decimal(1) / 60,
    "5m": Decimal(1) / 12,
    "15m": Decimal(1) / 4,
    "30m": Decimal(1) / 2,
    "1h": Decimal(1),
    "4h": Decimal(4),
    "1d": Decimal(24),
}
STARTING_EQUITY = Decimal("10000")
RISK_PER_TRADE_BPS = Decimal("50")
SPREAD_BPS = Decimal("1")
SLIPPAGE_BPS = Decimal("1")
FUNDING_PER_8H = Decimal("0.0001")


def bitunix_fee(schedules: list[FeeSchedule]) -> FeeSchedule:
    """The fee table is the source of truth (CLAUDE.md rule 6); its seed values
    (settings) only when the table has no Bitunix row yet."""
    rows = [s for s in schedules if s.exchange == "bitunix"]
    if rows:
        return max(rows, key=lambda s: s.effective_at)
    seed = next(r for r in get_settings().fee_schedule_seed() if r.exchange == "bitunix")
    return FeeSchedule(
        exchange="bitunix",
        vip_level=seed.vip_level,
        maker_bps=seed.maker_bps,
        taker_bps=seed.taker_bps,
        effective_at=datetime.now(UTC),
        source="settings_seed",
    )


def _f(v: Decimal | None) -> float | None:
    return None if v is None else float(v)


async def run_backtest(
    *,
    job_id: str,
    strategy: str,
    pair: str,
    timeframe: str,
    bars: int,
    fee: FeeSchedule,
) -> dict:
    klines, source = await fetch_history(pair, timeframe, bars)
    strat, params = build(strategy)
    model = ConservativeFillModel(
        fee_schedule=fee,
        spread_bps=SPREAD_BPS,
        slippage_bps=SLIPPAGE_BPS,
        funding_rate_per_interval=FUNDING_PER_8H,
        funding_interval_hours=8,
    )
    bt = Backtester(
        strat,
        model,
        starting_equity=STARTING_EQUITY,
        risk_per_trade_bps=RISK_PER_TRADE_BPS,
        bar_hours=_BAR_HOURS[timeframe],
        lookback=LOOKBACK_BARS,
    )
    # CPU-bound: off the event loop so the API keeps answering meanwhile.
    report = await asyncio.to_thread(bt.run, klines, params, pair=pair, timeframe=timeframe)
    m = report.metrics
    ending = report.ending_equity
    return {
        "job_id": job_id,
        "status": "done",
        "strategy": strategy,
        "pair": pair,
        "timeframe": timeframe,
        "bars_requested": bars,
        "bars": len(klines),
        "data_source": source,
        "from": klines[0].open_time.isoformat(),
        "to": klines[-1].open_time.isoformat(),
        "optimistic": True,
        "starting_equity": float(STARTING_EQUITY),
        "ending_equity": float(ending),
        "net_return_bps": float((ending - STARTING_EQUITY) / STARTING_EQUITY * 10000),
        "metrics": {
            "sample_size": m.sample_size,
            "win_rate": float(m.win_rate),
            "avg_win_bps": float(m.avg_win_bps),
            "avg_loss_bps": float(m.avg_loss_bps),
            "profit_factor": _f(m.profit_factor),
            "avg_rr": _f(m.avg_rr),
            "max_drawdown_bps": float(m.max_drawdown_bps),
            "fee_drag_bps": float(m.fee_drag_bps),
        },
        "assumptions": {
            "risk_per_trade_bps": float(RISK_PER_TRADE_BPS),
            "lookback_bars": LOOKBACK_BARS,
            "maker_bps": float(fee.maker_bps),
            "taker_bps": float(fee.taker_bps),
            "spread_bps": float(SPREAD_BPS),
            "slippage_bps": float(SLIPPAGE_BPS),
            "funding_per_8h": float(FUNDING_PER_8H),
        },
        "created_at": datetime.now(UTC).isoformat(),
    }
