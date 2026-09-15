from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from chalybcrypto_engine.backtest import (
    BacktestReport,
    Backtester,
    ConservativeFillModel,
    SimulatedTrade,
    simulate_entry_fill,
    simulate_exit_fill,
    summarize,
)
from chalybcrypto_engine.strategy import EmaAdxTrendParams, EmaAdxTrendStrategy
from chalybcrypto_shared import FeeSchedule, Side

from ._helpers import linear_trend, pullback_then_resume_uptrend


NOW = datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)


def _fees() -> FeeSchedule:
    return FeeSchedule(
        exchange="bitunix", symbol=None, vip_level="VIP0",
        maker_bps=Decimal("2"), taker_bps=Decimal("6"),
        effective_at=NOW, source="test",
    )


def _model(**overrides) -> ConservativeFillModel:
    base = dict(
        fee_schedule=_fees(),
        spread_bps=Decimal("1"),
        slippage_bps=Decimal("1"),
        use_taker_both_sides=True,
        funding_rate_per_interval=Decimal("0.0001"),
        funding_interval_hours=8,
    )
    base.update(overrides)
    return ConservativeFillModel(**base)


# ── conservative fills are conservative ───────────────────────────────────


def test_entry_long_pays_above_open_with_fees_and_slippage():
    series = linear_trend(2, start_price=100, step=1.0)
    bar = series[0]
    eff, fee, _slip = simulate_entry_fill(Side.LONG, Decimal("1"), bar, _model())
    assert eff > bar.open  # paid worse than open
    assert fee > 0


def test_entry_short_pays_below_open():
    series = linear_trend(2, start_price=100, step=1.0)
    bar = series[0]
    eff, fee, _ = simulate_entry_fill(Side.SHORT, Decimal("1"), bar, _model())
    assert eff < bar.open  # sold below open
    assert fee > 0


def test_exit_long_at_tp_takes_slippage_down():
    series = linear_trend(2, start_price=100, step=1.0)
    tp_price = Decimal("105")
    eff, fee = simulate_exit_fill(Side.LONG, Decimal("1"), tp_price, series[1], _model())
    assert eff < tp_price
    assert fee > 0


def test_no_slippage_no_spread_means_eff_equals_reference():
    series = linear_trend(2, start_price=100, step=1.0)
    model = _model(spread_bps=Decimal("0"), slippage_bps=Decimal("0"))
    eff, fee, _ = simulate_entry_fill(Side.LONG, Decimal("1"), series[0], model)
    assert eff == series[0].open
    assert fee > 0  # still pays fee


# ── metrics ───────────────────────────────────────────────────────────────


def _trade(pnl: Decimal, *, side: Side = Side.LONG, notional: Decimal = Decimal("1000"),
           fee: Decimal = Decimal("0"), funding: Decimal = Decimal("0")) -> SimulatedTrade:
    """Build a SimulatedTrade with a chosen net PnL."""
    entry = notional
    qty = Decimal("1")
    # entry=notional means qty=1 → notional=entry
    # gross = exit - entry (long); net = gross - fees - funding
    # want net = pnl ⇒ exit = pnl + fees + funding + entry
    exit_p = pnl + fee + funding + entry
    return SimulatedTrade(
        pair="BTCUSDT", side=side, qty=qty,
        entry_price=entry, exit_price=exit_p,
        entry_fee=fee / Decimal("2"), exit_fee=fee / Decimal("2"),
        funding_paid=funding, slippage_cost=Decimal("0"),
        opened_at=NOW, closed_at=NOW, exit_reason="tp" if pnl > 0 else "sl",
    )


def test_summarize_empty_returns_zero_sized_metrics():
    m = summarize([], starting_equity=Decimal("10000"))
    assert m.sample_size == 0
    assert m.optimistic is True


def test_summarize_win_rate_and_profit_factor():
    trades = [
        _trade(Decimal("100")),
        _trade(Decimal("100")),
        _trade(Decimal("-50")),
        _trade(Decimal("-50")),
    ]
    m = summarize(trades, starting_equity=Decimal("10000"))
    assert m.sample_size == 4
    assert m.win_rate == Decimal("0.5")
    assert m.profit_factor == Decimal("2")  # 200 wins / 100 losses
    assert m.optimistic is True


def test_summarize_fee_drag_is_total_costs_over_total_notional():
    trades = [_trade(Decimal("0"), fee=Decimal("2"), funding=Decimal("1"))]
    m = summarize(trades, starting_equity=Decimal("10000"))
    # notional 1000, total costs 3 → 30 bps
    assert m.fee_drag_bps == Decimal("30")


def test_summarize_max_drawdown_tracks_peak_minus_trough():
    trades = [_trade(Decimal("100")), _trade(Decimal("-200")), _trade(Decimal("50"))]
    m = summarize(trades, starting_equity=Decimal("10000"))
    # Equity path: 10000 → 10100 (peak) → 9900 → 9950
    # Max DD: (10100 - 9900) / 10100 ≈ 0.0198 ≈ 198 bps
    assert m.max_drawdown_bps > Decimal("197") and m.max_drawdown_bps < Decimal("199")


# ── end-to-end backtest ───────────────────────────────────────────────────


def test_end_to_end_backtest_runs_and_labels_optimistic():
    strat = EmaAdxTrendStrategy()
    bt = Backtester(strat, _model(), starting_equity=Decimal("10000"))
    klines = pullback_then_resume_uptrend(n_pre=220, n_pull=6, n_rip=60)
    report: BacktestReport = bt.run(klines, EmaAdxTrendParams(adx_threshold=Decimal("15")),
                                    pair="BTCUSDT")
    assert isinstance(report, BacktestReport)
    assert report.optimistic is True
    assert report.metrics.optimistic is True
    assert report.strategy_key == "ema_adx_trend"
    # The strategy should have triggered at least once on this constructed setup.
    assert report.metrics.sample_size >= 1
    # Every trade carries fees and (non-zero) funding given the model.
    for t in report.trades:
        assert t.total_fees > 0
        assert t.funding_paid > 0
        # entry must be worse than the bar's open (conservative).
        # We can only check this consistency: net == gross - fees - funding.
        assert t.net_pnl == t.gross_pnl - t.total_fees - t.funding_paid
