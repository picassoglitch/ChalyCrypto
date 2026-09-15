from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from chalybcrypto_shared import (
    FeeSchedule,
    Kline,
    MarginType,
    MarketSnapshot,
    Mode,
    OrderBookLevel,
    OrderBookSnapshot,
    OrderType,
    RiskProfile,
    Settings,
    Side,
    Signal,
    TradeDecision,
    dedup_hash,
    seed_fee_schedules,
)


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _roundtrip(obj):
    """Dump → reload, return reloaded instance for equality check."""
    cls = type(obj)
    return cls.model_validate(obj.model_dump(mode="json"))


def test_fee_schedule_roundtrip():
    f = FeeSchedule(
        exchange="bitunix",
        symbol="BTCUSDT",
        vip_level="VIP0",
        maker_bps=Decimal("2.0"),
        taker_bps=Decimal("6.0"),
        effective_at=NOW,
        source="config_seed",
    )
    assert _roundtrip(f) == f


def test_kline_roundtrip():
    k = Kline(
        open_time=NOW,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=Decimal("1234.5"),
        close_time=NOW,
    )
    assert _roundtrip(k) == k


def test_market_snapshot_roundtrip():
    snap = MarketSnapshot(
        pair="BTCUSDT",
        exchange="bitunix",
        taken_at=NOW,
        klines=[
            Kline(
                open_time=NOW,
                open=Decimal("1"),
                high=Decimal("2"),
                low=Decimal("0.5"),
                close=Decimal("1.5"),
                volume=Decimal("10"),
                close_time=NOW,
            )
        ],
        order_book=OrderBookSnapshot(
            exchange="bitunix",
            pair="BTCUSDT",
            taken_at=NOW,
            bids=[OrderBookLevel(price=Decimal("100"), size=Decimal("1"))],
            asks=[OrderBookLevel(price=Decimal("101"), size=Decimal("1"))],
            is_native=True,
        ),
        mark_price=Decimal("100.5"),
        funding_rate=Decimal("0.0001"),
        open_interest=Decimal("123456"),
        spread_bps=Decimal("1.0"),
        coinglass_context={"oi_dominance": "0.31"},
        cmc_context={"rank": 1},
        indicators={"adx": "27.5"},
    )
    assert _roundtrip(snap) == snap


def test_signal_roundtrip():
    sig = Signal(
        id=uuid4(),
        pair="BTCUSDT",
        side=Side.LONG,
        strategy="ema_trend",
        entry=Decimal("100"),
        stop_loss=Decimal("99"),
        take_profits=[Decimal("102"), Decimal("104")],
        leverage=Decimal("10"),
        margin_type=MarginType.ISOLATED,
        timeframe="5m",
        thesis_tags=["adx_strong", "ema_aligned"],
        source="scanner",
        dedup_hash=dedup_hash("BTCUSDT", "long", "ema_trend", Decimal("100"), Decimal("99")),
        created_at=NOW,
    )
    assert _roundtrip(sig) == sig


def test_risk_profile_roundtrip():
    rp = RiskProfile(
        name="default",
        max_risk_per_trade_bps=Decimal("50"),
        max_daily_loss_bps=Decimal("300"),
        max_weekly_loss_bps=Decimal("800"),
        max_drawdown_bps=Decimal("1500"),
        max_open_positions=3,
        max_leverage=Decimal("20"),
        max_exposure_per_asset_bps=Decimal("3000"),
        max_total_exposure_bps=Decimal("8000"),
        max_trades_per_hour=6,
        min_rr=Decimal("1.5"),
        min_adx=Decimal("20"),
        min_liquidity_usd=Decimal("250000"),
        min_volume_usd=Decimal("1000000"),
        min_expected_profit_after_fees_bps=Decimal("10"),
        min_liquidation_distance_bps=Decimal("200"),
        stale_price_max_seconds=5,
        cooldown_after_loss_seconds=900,
        cooldown_after_volatility_spike_seconds=300,
        breakeven_trigger_bps=Decimal("30"),
        trailing_trigger_bps=Decimal("60"),
        partial_tp_trigger_bps=Decimal("40"),
    )
    assert _roundtrip(rp) == rp


def test_trade_decision_roundtrip():
    td = TradeDecision(
        signal_id=uuid4(),
        mode=Mode.PAPER,
        approved=False,
        reason="ev_negative_after_costs",
        intended_order_type=OrderType.MARKET,
        intended_qty=Decimal("0.001"),
        intended_entry=Decimal("100"),
        intended_stop_loss=Decimal("99"),
        intended_take_profits=[Decimal("102")],
        intended_leverage=Decimal("10"),
        ev_net_bps=Decimal("-3"),
        liquidation_price=Decimal("90"),
        liquidation_distance_bps=Decimal("1000"),
        fees_round_trip_bps=Decimal("12"),
        idempotency_key=dedup_hash("BTCUSDT", "long", NOW.isoformat()),
        decided_at=NOW,
    )
    assert _roundtrip(td) == td


def test_dedup_hash_stable_and_normalized():
    h1 = dedup_hash("BTCUSDT", "long", Decimal("1.50"))
    h2 = dedup_hash("btcusdt", "LONG", Decimal("1.5"))
    assert h1 == h2
    assert len(h1) == 64


def test_dedup_hash_order_sensitive():
    assert dedup_hash("a", "b") != dedup_hash("b", "a")


def test_settings_loads_with_defaults(monkeypatch):
    for k in list(monkeypatch.__dict__):
        pass
    s = Settings(_env_file=None)
    assert s.app_env == "local"
    assert s.supabase_db_schema == "chalybcrypto"
    assert s.fee_bitunix_taker_bps == Decimal("6.0")


def test_fee_schedule_seed_produces_three_rows():
    s = Settings(_env_file=None)
    rows = seed_fee_schedules(s, now=NOW)
    assert {r.exchange for r in rows} == {"binance", "lbank", "bitunix"}
    assert all(r.source == "config_seed" for r in rows)
    bitunix = next(r for r in rows if r.exchange == "bitunix")
    assert bitunix.vip_level == "VIP0"
    assert bitunix.taker_bps == Decimal("6.0")


def test_models_reject_extra_fields():
    with pytest.raises(Exception):
        Kline.model_validate(
            {
                "open_time": NOW.isoformat(),
                "open": "1",
                "high": "2",
                "low": "0.5",
                "close": "1.5",
                "volume": "10",
                "close_time": NOW.isoformat(),
                "secret_field": "x",
            }
        )
