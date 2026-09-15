"""PgStore tests — same contract as InMemoryStore, against a real Postgres.

Uses the session-scoped db_dsn fixture from conftest.py which spins up a fresh DB,
applies auth shim + migrations, and yields a DSN. Tests run as superuser (RLS bypassed,
which is how the API runs in production via the service role).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from chalybcrypto_api.pg_store import PgStore
from chalybcrypto_shared import (
    FeeSchedule,
    MarginType,
    Mode,
    Side,
    Signal,
    TradeDecision,
    dedup_hash,
)


NOW = datetime(2026, 6, 6, tzinfo=timezone.utc)


def _signal(pair: str = "BTCUSDT") -> Signal:
    return Signal(
        pair=pair,
        side=Side.LONG,
        strategy="ema_adx_trend",
        entry=Decimal("60000"),
        stop_loss=Decimal("59700"),
        take_profits=[Decimal("60900")],
        leverage=Decimal("10"),
        margin_type=MarginType.ISOLATED,
        timeframe="5m",
        thesis_tags=[],
        source="scanner",
        dedup_hash=dedup_hash(pair, "long"),
        created_at=NOW,
    )


def _decision(sig: Signal, approved: bool = True, reason: str = "ok") -> TradeDecision:
    return TradeDecision(
        signal_id=sig.id,
        mode=Mode.PAPER,
        approved=approved,
        reason=reason,
        intended_take_profits=[],
        idempotency_key=dedup_hash(sig.dedup_hash, reason),
        decided_at=NOW,
    )


@pytest.fixture
async def store(db_dsn) -> PgStore:
    return PgStore(db_dsn)


async def test_strategies_seeds_mvp_three_if_empty(store):
    rows = await store.list_strategies()
    keys = {r["key"] for r in rows}
    assert keys == {"ema_adx_trend", "vwap_rsi_meanrev", "fvg_ob"}


async def test_get_mode_defaults_to_paper(store):
    user = uuid4()
    mode = await store.get_mode(user_id=user)
    assert mode["mode"] == Mode.PAPER.value
    assert mode["live_unlocked"] is False


async def test_set_mode_paper_persists(store):
    user = uuid4()
    out = await store.set_mode(user_id=user, mode=Mode.PAPER)
    assert out["mode"] == "paper"
    again = await store.get_mode(user_id=user)
    assert again["mode"] == "paper"


async def test_set_mode_semi_auto_blocked_until_unlock(store):
    user = uuid4()
    with pytest.raises(PermissionError, match="paper_gate_unmet"):
        await store.set_mode(user_id=user, mode=Mode.SEMI_AUTO)
    await store.unlock_live_for_test(user_id=user)
    out = await store.set_mode(user_id=user, mode=Mode.SEMI_AUTO)
    assert out["mode"] == "semi_auto"


async def test_set_mode_full_auto_disabled_in_mvp(store):
    user = uuid4()
    await store.unlock_live_for_test(user_id=user)
    with pytest.raises(PermissionError, match="full_auto_disabled_in_mvp"):
        await store.set_mode(user_id=user, mode=Mode.FULL_AUTO)


async def test_add_parsed_signal_persists(store):
    user = uuid4()
    sig = _signal()
    rec = await store.add_parsed_signal(user_id=user, signal=sig, raw_text="raw")
    assert rec["pair"] == "BTCUSDT"
    assert rec["side"] == "long"
    assert rec["status"] == "parsed"

    rows = await store.list_signals(user_id=user)
    assert len(rows) == 1


async def test_add_validated_signal_records_decision(store):
    user = uuid4()
    sig = _signal()
    rejected = _decision(sig, approved=False, reason="ev_negative_after_costs")
    rec = await store.add_validated_signal(user_id=user, decision=rejected)
    assert rec["status"] == "rejected"
    assert rec["reject_reason"] == "ev_negative_after_costs"


async def test_list_signals_status_filter(store):
    user = uuid4()
    sig = _signal()
    await store.add_parsed_signal(user_id=user, signal=sig)
    await store.add_validated_signal(user_id=user, decision=_decision(sig, approved=False, reason="min_rr_not_met"))

    parsed_only = await store.list_signals(user_id=user, status="parsed")
    rejected_only = await store.list_signals(user_id=user, status="rejected")
    assert len(parsed_only) == 1
    assert len(rejected_only) == 1


async def test_signals_are_per_user(store):
    a = uuid4()
    b = uuid4()
    await store.add_parsed_signal(user_id=a, signal=_signal("BTCUSDT"))
    await store.add_parsed_signal(user_id=b, signal=_signal("ETHUSDT"))
    a_rows = await store.list_signals(user_id=a)
    b_rows = await store.list_signals(user_id=b)
    assert len(a_rows) == 1 and a_rows[0]["pair"] == "BTCUSDT"
    assert len(b_rows) == 1 and b_rows[0]["pair"] == "ETHUSDT"


async def test_fee_schedules_round_trip(store):
    fee = FeeSchedule(
        exchange="bitunix", symbol=None, vip_level="VIP0",
        maker_bps=Decimal("2"), taker_bps=Decimal("6"),
        effective_at=NOW, source="seed",
    )
    out = await store.put_fee_schedules(schedules=[fee])
    assert len(out) == 1
    assert out[0].exchange == "bitunix"

    listed = await store.list_fee_schedules()
    assert len(listed) == 1


async def test_positions_and_trades_empty_initially(store):
    user = uuid4()
    assert await store.list_positions(user_id=user) == []
    assert await store.list_trades(user_id=user) == []
