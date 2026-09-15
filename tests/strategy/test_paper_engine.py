"""Paper engine — end-to-end snapshot → strategy → risk → simulated fill loop."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from chalybcrypto_engine.backtest.fills import ConservativeFillModel
from chalybcrypto_engine.paper import PaperEngine, PaperTick
from chalybcrypto_engine.risk import InMemoryIdempotencyStore, RiskEngine, StrategyStats
from chalybcrypto_engine.risk.ev import EVInputs
from chalybcrypto_engine.strategy import (
    EmaAdxTrendParams,
    EmaAdxTrendStrategy,
)
from chalybcrypto_shared import FeeSchedule, MarketSnapshot

from ._helpers import pullback_then_resume_uptrend, linear_trend


NOW = datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)


def _fees() -> FeeSchedule:
    return FeeSchedule(
        exchange="bitunix", symbol=None, vip_level="VIP0",
        maker_bps=Decimal("2"), taker_bps=Decimal("2"),  # low so EV passes
        effective_at=NOW, source="test",
    )


def _model() -> ConservativeFillModel:
    return ConservativeFillModel(
        fee_schedule=_fees(),
        spread_bps=Decimal("1"),
        slippage_bps=Decimal("1"),
        funding_rate_per_interval=Decimal("0"),
    )


def _ev_inputs() -> EVInputs:
    return EVInputs(
        fee_schedule=_fees(),
        use_taker_both_sides=True,
        expected_spread_bps=Decimal("0"),
        expected_slippage_bps=Decimal("0"),
        expected_hold_hours=Decimal("0.5"),
        funding_rate=Decimal("0"),
        funding_interval_hours=8,
    )


def _stats():
    return StrategyStats(
        strategy="ema_adx_trend",
        sample_size=100,
        win_rate=Decimal("0.55"),
        avg_win_bps=Decimal("80"),
        avg_loss_bps=Decimal("40"),
    )


def _account():
    from chalybcrypto_engine.risk import AccountState

    return AccountState(
        equity=Decimal("10000"),
        balance=Decimal("10000"),
        peak_equity=Decimal("10000"),
        last_tick_at=NOW,
        paper_gate_unlocked=True,
    )


def _profile():
    from chalybcrypto_shared import RiskProfile

    return RiskProfile(
        name="test",
        max_risk_per_trade_bps=Decimal("50"),
        max_daily_loss_bps=Decimal("300"),
        max_weekly_loss_bps=Decimal("800"),
        max_drawdown_bps=Decimal("1500"),
        max_open_positions=3,
        max_leverage=Decimal("20"),
        max_exposure_per_asset_bps=Decimal("3000"),
        max_total_exposure_bps=Decimal("8000"),
        max_trades_per_hour=6,
        min_rr=Decimal("1.4"),
        min_adx=Decimal("20"),
        min_liquidity_usd=Decimal("0"),
        min_volume_usd=Decimal("0"),
        min_expected_profit_after_fees_bps=Decimal("5"),
        min_liquidation_distance_bps=Decimal("200"),
        stale_price_max_seconds=120,
        cooldown_after_loss_seconds=900,
        cooldown_after_volatility_spike_seconds=300,
        breakeven_trigger_bps=Decimal("30"),
        trailing_trigger_bps=Decimal("60"),
        partial_tp_trigger_bps=Decimal("40"),
    )


async def test_paper_tick_with_no_signal_records_no_signal_decision():
    eng = PaperEngine(EmaAdxTrendStrategy(), _model())
    series = linear_trend(220, start_price=100, step=1.0)  # no cross, no signal
    snap = MarketSnapshot(
        pair="BTCUSDT", exchange="binance", taken_at=series[-1].close_time,
        klines=series, mark_price=series[-1].close,
    )
    next_bar = series[-1]
    tick = PaperTick(snapshot=snap, next_bar=next_bar)
    result = await eng.tick(
        tick,
        params=EmaAdxTrendParams(),
        risk_profile=_profile(),
        account=_account(),
        ev_inputs=_ev_inputs(),
        strategy_stats=_stats(),
        idempotency_store=InMemoryIdempotencyStore(),
        now=NOW,
    )
    assert result.signal is None
    assert result.entry_fill is None
    assert result.decision.reason == "no_signal"
    assert result.decision.actor == "paper_engine"


async def test_paper_tick_approved_signal_produces_simulated_entry():
    eng = PaperEngine(EmaAdxTrendStrategy(), _model())
    series = pullback_then_resume_uptrend()  # cross-up on last bar
    snap = MarketSnapshot(
        pair="BTCUSDT", exchange="binance", taken_at=series[-1].close_time,
        klines=series, mark_price=series[-1].close,
    )
    next_bar = series[-1]  # the bar to fill into
    tick = PaperTick(snapshot=snap, next_bar=next_bar)
    result = await eng.tick(
        tick,
        params=EmaAdxTrendParams(adx_threshold=Decimal("15")),
        risk_profile=_profile(),
        account=_account(),
        ev_inputs=_ev_inputs(),
        strategy_stats=_stats(),
        idempotency_store=InMemoryIdempotencyStore(),
        now=NOW,
    )
    assert result.signal is not None
    assert result.decision.approved is True
    assert result.entry_fill is not None
    # Conservative: entry filled WORSE than next_bar.open.
    assert result.entry_fill.entry_price > next_bar.open
    assert result.entry_fill.entry_fee > 0
    assert result.entry_fill.exit_reason == "open"


async def test_paper_tick_rejection_returns_reason_no_fill():
    eng = PaperEngine(EmaAdxTrendStrategy(), _model())
    series = pullback_then_resume_uptrend()
    snap = MarketSnapshot(
        pair="BTCUSDT", exchange="binance", taken_at=series[-1].close_time,
        klines=series, mark_price=series[-1].close,
    )
    tick = PaperTick(snapshot=snap, next_bar=series[-1])
    locked_account = _account().model_copy(update={"account_protection_lock": True})
    result = await eng.tick(
        tick,
        params=EmaAdxTrendParams(adx_threshold=Decimal("15")),
        risk_profile=_profile(),
        account=locked_account,
        ev_inputs=_ev_inputs(),
        strategy_stats=_stats(),
        idempotency_store=InMemoryIdempotencyStore(),
        now=NOW,
    )
    assert result.signal is not None  # signal was generated
    assert result.decision.approved is False
    assert result.decision.reason == "account_protection_lock"
    assert result.entry_fill is None  # nothing filled
