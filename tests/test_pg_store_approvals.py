"""PgStore approvals — real Postgres-backed queue.

Migration 0003 must be applied (the conftest fixture does this automatically).
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from chalybcrypto_api.pg_store import PgStore
from chalybcrypto_shared import (
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
        dedup_hash=dedup_hash(pair, "long", str(uuid4())),  # uniquify across tests
        created_at=NOW,
    )


def _decision(sig: Signal, idem_key: str | None = None) -> TradeDecision:
    return TradeDecision(
        signal_id=sig.id,
        mode=Mode.SEMI_AUTO,
        approved=True,
        reason="ok",
        intended_qty=Decimal("0.01"),
        intended_leverage=Decimal("10"),
        intended_take_profits=[Decimal("60900")],
        ev_net_bps=Decimal("15"),
        liquidation_distance_bps=Decimal("950"),
        idempotency_key=idem_key or dedup_hash(sig.dedup_hash, "ok"),
        decided_at=NOW,
    )


@pytest.fixture
async def store(db_dsn) -> PgStore:
    return PgStore(db_dsn)


async def test_add_approval_persists_pending(store):
    user = uuid4()
    sig = _signal()
    rec = await store.add_approval(user_id=user, signal=sig, decision=_decision(sig))
    assert rec["status"] == "pending"
    assert rec["pair"] == "BTCUSDT"
    assert rec["side"] == "long"


async def test_list_approvals_returns_pending_only(store):
    user = uuid4()
    sig = _signal()
    await store.add_approval(user_id=user, signal=sig, decision=_decision(sig))
    rows = await store.list_approvals(user_id=user)
    assert len(rows) == 1
    assert rows[0]["pair"] == "BTCUSDT"


async def test_add_approval_is_idempotent_on_idempotency_key(store):
    """Re-running a scanner tick should not produce duplicate approvals."""
    user = uuid4()
    sig = _signal()
    dec = _decision(sig)
    a = await store.add_approval(user_id=user, signal=sig, decision=dec)
    b = await store.add_approval(user_id=user, signal=sig, decision=dec)
    assert a["id"] == b["id"]
    rows = await store.list_approvals(user_id=user)
    assert len(rows) == 1


async def test_resolve_approval_flips_status_and_stamps_resolved_at(store):
    user = uuid4()
    sig = _signal()
    rec = await store.add_approval(user_id=user, signal=sig, decision=_decision(sig))
    aid = UUID(rec["id"]) if isinstance(rec["id"], str) else rec["id"]

    out = await store.resolve_approval(
        user_id=user, approval_id=aid, action="approve", reason="looks clean"
    )
    assert out is not None
    assert out["status"] == "approved"
    assert out["resolved_by"] == "human"
    assert out["resolution_reason"] == "looks clean"

    # Queue is empty after resolution.
    assert await store.list_approvals(user_id=user) == []


async def test_resolve_approval_unknown_action_returns_none(store):
    user = uuid4()
    sig = _signal()
    rec = await store.add_approval(user_id=user, signal=sig, decision=_decision(sig))
    aid = UUID(rec["id"]) if isinstance(rec["id"], str) else rec["id"]

    out = await store.resolve_approval(user_id=user, approval_id=aid, action="yolo")
    assert out is None
    # Original row still pending.
    assert len(await store.list_approvals(user_id=user)) == 1


async def test_resolve_approval_404_when_not_owned(store):
    """Cross-user resolution is invisible (RLS would block on real Supabase; we run as
    superuser in tests but the WHERE clause enforces ownership)."""
    owner = uuid4()
    intruder = uuid4()
    sig = _signal()
    rec = await store.add_approval(user_id=owner, signal=sig, decision=_decision(sig))
    aid = UUID(rec["id"]) if isinstance(rec["id"], str) else rec["id"]

    out = await store.resolve_approval(user_id=intruder, approval_id=aid, action="approve")
    assert out is None


async def test_resolve_approval_each_action_maps_to_state(store):
    """All six valid actions flip status correctly."""
    user = uuid4()
    expected = {
        "approve": "approved",
        "reject": "rejected",
        "continue": "continued",
        "close": "closed",
        "breakeven": "breakeven",
        "protect": "protected",
    }
    for action, target_status in expected.items():
        sig = _signal()  # fresh dedup_hash via uuid4 inside
        rec = await store.add_approval(user_id=user, signal=sig, decision=_decision(sig))
        aid = UUID(rec["id"]) if isinstance(rec["id"], str) else rec["id"]
        out = await store.resolve_approval(user_id=user, approval_id=aid, action=action)
        assert out is not None and out["status"] == target_status, (
            f"action {action!r} should map to {target_status!r}, got {out['status'] if out else None!r}"
        )


async def test_approvals_isolated_per_user(store):
    a = uuid4()
    b = uuid4()
    await store.add_approval(user_id=a, signal=_signal("BTCUSDT"), decision=_decision(_signal("BTCUSDT")))
    await store.add_approval(user_id=b, signal=_signal("ETHUSDT"), decision=_decision(_signal("ETHUSDT")))
    a_rows = await store.list_approvals(user_id=a)
    b_rows = await store.list_approvals(user_id=b)
    assert len(a_rows) == 1
    assert len(b_rows) == 1
    assert a_rows[0]["pair"] != b_rows[0]["pair"]
