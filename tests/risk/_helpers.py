"""Shared test fixtures/builders for risk-engine tests.

Keeps each test file focused on the behavior it owns.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from chalybcrypto_engine.risk import AccountState, StrategyStats
from chalybcrypto_engine.risk.ev import EVInputs
from chalybcrypto_shared import (
    FeeSchedule,
    MarginType,
    RiskProfile,
    Side,
    Signal,
    dedup_hash,
)


NOW = datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)


def make_signal(
    *,
    pair: str = "BTCUSDT",
    side: Side = Side.LONG,
    entry: Decimal = Decimal("60000"),
    stop_loss: Decimal = Decimal("59700"),
    take_profits: list[Decimal] | None = None,
    leverage: Decimal = Decimal("10"),
    strategy: str = "ema_trend",
) -> Signal:
    return Signal(
        id=uuid4(),
        pair=pair,
        side=side,
        strategy=strategy,
        entry=entry,
        stop_loss=stop_loss,
        take_profits=take_profits or [Decimal("60900")],  # 3x stop = 1.5 RR vs default min 1.5
        leverage=leverage,
        margin_type=MarginType.ISOLATED,
        timeframe="5m",
        thesis_tags=[],
        source="scanner",
        dedup_hash=dedup_hash(pair, side.value, entry),
        created_at=NOW,
    )


def make_account(
    *,
    equity: Decimal = Decimal("10000"),
    peak_equity: Decimal | None = None,
    daily_pnl: Decimal = Decimal("0"),
    weekly_pnl: Decimal = Decimal("0"),
    open_positions: int = 0,
    total_exposure_bps: Decimal = Decimal("0"),
    trades_last_hour: int = 0,
    locked: bool = False,
    paper_gate_unlocked: bool = True,
    last_tick_at: datetime | None = NOW,
    last_loss_at: datetime | None = None,
    last_volatility_at: datetime | None = None,
) -> AccountState:
    return AccountState(
        equity=equity,
        balance=equity,
        peak_equity=peak_equity if peak_equity is not None else equity,
        daily_realized_pnl=daily_pnl,
        weekly_realized_pnl=weekly_pnl,
        open_positions_count=open_positions,
        total_exposure_bps=total_exposure_bps,
        trades_last_hour=trades_last_hour,
        account_protection_lock=locked,
        paper_gate_unlocked=paper_gate_unlocked,
        last_tick_at=last_tick_at,
        last_loss_at=last_loss_at,
        last_volatility_spike_at=last_volatility_at,
    )


def make_profile(
    *,
    max_risk_bps: Decimal = Decimal("50"),
    max_daily_loss_bps: Decimal = Decimal("300"),
    max_weekly_loss_bps: Decimal = Decimal("800"),
    max_drawdown_bps: Decimal = Decimal("1500"),
    max_open_positions: int = 3,
    max_leverage: Decimal = Decimal("20"),
    max_per_asset_bps: Decimal = Decimal("3000"),
    max_total_bps: Decimal = Decimal("8000"),
    max_trades_per_hour: int = 6,
    min_rr: Decimal = Decimal("1.5"),
    min_liquidation_distance_bps: Decimal = Decimal("200"),
    stale_max_seconds: int = 5,
    cooldown_loss_seconds: int = 900,
    cooldown_vol_seconds: int = 300,
    min_expected_profit_bps: Decimal = Decimal("5"),
) -> RiskProfile:
    return RiskProfile(
        name="test",
        max_risk_per_trade_bps=max_risk_bps,
        max_daily_loss_bps=max_daily_loss_bps,
        max_weekly_loss_bps=max_weekly_loss_bps,
        max_drawdown_bps=max_drawdown_bps,
        max_open_positions=max_open_positions,
        max_leverage=max_leverage,
        max_exposure_per_asset_bps=max_per_asset_bps,
        max_total_exposure_bps=max_total_bps,
        max_trades_per_hour=max_trades_per_hour,
        min_rr=min_rr,
        min_adx=Decimal("20"),
        min_liquidity_usd=Decimal("0"),
        min_volume_usd=Decimal("0"),
        min_expected_profit_after_fees_bps=min_expected_profit_bps,
        min_liquidation_distance_bps=min_liquidation_distance_bps,
        stale_price_max_seconds=stale_max_seconds,
        cooldown_after_loss_seconds=cooldown_loss_seconds,
        cooldown_after_volatility_spike_seconds=cooldown_vol_seconds,
        breakeven_trigger_bps=Decimal("30"),
        trailing_trigger_bps=Decimal("60"),
        partial_tp_trigger_bps=Decimal("40"),
    )


def make_stats(
    *,
    sample_size: int = 100,
    win_rate: Decimal = Decimal("0.55"),
    avg_win_bps: Decimal = Decimal("60"),
    avg_loss_bps: Decimal = Decimal("40"),
) -> StrategyStats:
    return StrategyStats(
        strategy="ema_trend",
        sample_size=sample_size,
        win_rate=win_rate,
        avg_win_bps=avg_win_bps,
        avg_loss_bps=avg_loss_bps,
    )


def make_ev_inputs(
    *,
    taker_bps: Decimal = Decimal("6"),
    maker_bps: Decimal = Decimal("2"),
    spread_bps: Decimal = Decimal("1"),
    slippage_bps: Decimal = Decimal("1"),
    hold_hours: Decimal = Decimal("0.05"),
    funding_rate: Decimal = Decimal("0.0001"),
) -> EVInputs:
    fee = FeeSchedule(
        exchange="bitunix",
        symbol=None,
        vip_level="VIP0",
        maker_bps=maker_bps,
        taker_bps=taker_bps,
        effective_at=NOW,
        source="test",
    )
    return EVInputs(
        fee_schedule=fee,
        use_taker_both_sides=True,
        expected_spread_bps=spread_bps,
        expected_slippage_bps=slippage_bps,
        expected_hold_hours=hold_hours,
        funding_rate=funding_rate,
        funding_interval_hours=8,
    )
