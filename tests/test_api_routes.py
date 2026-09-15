"""API route tests against TestClient — exercise the full surface from BUILD_PLAN."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from chalybcrypto_api.deps import set_store_for_tests
from chalybcrypto_api.main import app
from chalybcrypto_api.store import InMemoryStore
from chalybcrypto_engine.risk import AccountState
from chalybcrypto_shared import (
    FeeSchedule,
    MarginType,
    Mode,
    RiskProfile,
    Side,
    Signal,
    TradeDecision,
    dedup_hash,
)


NOW = datetime(2026, 6, 6, tzinfo=timezone.utc)
USER = UUID("11111111-1111-1111-1111-111111111111")


@pytest.fixture
def client():
    store = InMemoryStore()
    set_store_for_tests(store)
    with TestClient(app) as c:
        yield c, store


def _auth():
    return {"X-User-Id": str(USER)}


# ── auth stub ──────────────────────────────────────────────────────────────


def test_missing_x_user_id_returns_401(client):
    c, _ = client
    r = c.get("/api/signals")
    assert r.status_code == 401


def test_bad_x_user_id_returns_400(client):
    c, _ = client
    r = c.get("/api/signals", headers={"X-User-Id": "not-a-uuid"})
    assert r.status_code == 400


# ── signals ────────────────────────────────────────────────────────────────


def test_list_signals_empty(client):
    c, _ = client
    r = c.get("/api/signals", headers=_auth())
    assert r.status_code == 200
    assert r.json() == []


def _signal() -> Signal:
    return Signal(
        pair="BTCUSDT", side=Side.LONG, strategy="ema_adx_trend",
        entry=Decimal("60000"), stop_loss=Decimal("59700"),
        take_profits=[Decimal("60900")], leverage=Decimal("10"),
        margin_type=MarginType.ISOLATED, timeframe="5m",
        thesis_tags=[], source="scanner",
        dedup_hash=dedup_hash("BTCUSDT", "long"), created_at=NOW,
    )


async def test_list_signals_after_add(client):
    c, store = client
    sig = _signal()
    await store.add_parsed_signal(user_id=USER, signal=sig, raw_text="raw")
    r = c.get("/api/signals", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["pair"] == "BTCUSDT"
    assert body[0]["status"] == "parsed"


async def test_signals_status_filter(client):
    c, store = client
    sig = _signal()
    await store.add_parsed_signal(user_id=USER, signal=sig)
    # Add a rejected validated signal too.
    td = TradeDecision(
        signal_id=sig.id, mode=Mode.PAPER, approved=False, reason="ev_negative_after_costs",
        intended_take_profits=[], idempotency_key=dedup_hash("x"), decided_at=NOW,
    )
    await store.add_validated_signal(user_id=USER, decision=td)

    r = c.get("/api/signals?status=parsed", headers=_auth())
    assert len(r.json()) == 1
    r = c.get("/api/signals?status=rejected", headers=_auth())
    assert len(r.json()) == 1
    r = c.get("/api/signals?status=approved", headers=_auth())
    assert r.json() == []


# ── approvals ──────────────────────────────────────────────────────────────


async def test_approvals_round_trip(client):
    c, store = client
    sig = _signal()
    td = TradeDecision(
        signal_id=sig.id, mode=Mode.SEMI_AUTO, approved=True, reason="ok",
        intended_qty=Decimal("0.01"), intended_leverage=Decimal("10"),
        intended_take_profits=[], idempotency_key=dedup_hash("x"), decided_at=NOW,
    )
    record = await store.add_approval(user_id=USER, signal=sig, decision=td)

    r = c.get("/api/approvals", headers=_auth())
    assert r.status_code == 200
    assert len(r.json()) == 1

    r = c.post(
        f"/api/approvals/{record['id']}/decision",
        headers=_auth(),
        json={"action": "approve"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "approve"

    # Now the queue is empty again (only 'pending' approvals returned).
    r = c.get("/api/approvals", headers=_auth())
    assert r.json() == []


def test_approval_decision_rejects_unknown_action(client):
    c, _ = client
    r = c.post(
        f"/api/approvals/{uuid4()}/decision",
        headers=_auth(),
        json={"action": "yolo"},
    )
    assert r.status_code == 400


def test_approval_decision_404_when_missing(client):
    c, _ = client
    r = c.post(
        f"/api/approvals/{uuid4()}/decision",
        headers=_auth(),
        json={"action": "approve"},
    )
    assert r.status_code == 404


# ── mode (paper-gate enforcement) ──────────────────────────────────────────


def test_get_mode_defaults_to_paper(client):
    c, _ = client
    r = c.get("/api/mode", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "paper"
    assert body["live_unlocked"] is False


def test_put_mode_semi_auto_blocked_until_paper_gate(client):
    c, _ = client
    r = c.put("/api/mode", headers=_auth(), json={"mode": "semi_auto"})
    assert r.status_code == 403
    assert "paper_gate_unmet" in r.json()["detail"]


async def test_put_mode_full_auto_is_disabled_in_mvp(client):
    c, store = client
    await store.unlock_live_for_test(user_id=USER)
    r = c.put("/api/mode", headers=_auth(), json={"mode": "full_auto"})
    assert r.status_code == 403
    assert "full_auto_disabled_in_mvp" in r.json()["detail"]


async def test_put_mode_semi_auto_succeeds_after_unlock(client):
    c, store = client
    await store.unlock_live_for_test(user_id=USER)
    r = c.put("/api/mode", headers=_auth(), json={"mode": "semi_auto"})
    assert r.status_code == 200
    assert r.json()["mode"] == "semi_auto"


# ── risk profiles ──────────────────────────────────────────────────────────


def _risk_profile() -> RiskProfile:
    return RiskProfile(
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


def test_risk_profile_put_then_get(client):
    c, _ = client
    profile = _risk_profile()
    r = c.put("/api/risk-profiles", headers=_auth(), json=profile.model_dump(mode="json"))
    assert r.status_code == 200
    assert r.json()["name"] == "default"

    r = c.get("/api/risk-profiles", headers=_auth())
    assert r.status_code == 200
    assert r.json()["name"] == "default"


def test_risk_profile_get_returns_none_initially(client):
    c, _ = client
    r = c.get("/api/risk-profiles", headers=_auth())
    assert r.status_code == 200
    assert r.json() is None


# ── fees + strategies ──────────────────────────────────────────────────────


def test_fee_schedules_put_then_get(client):
    c, _ = client
    fee = FeeSchedule(
        exchange="bitunix", symbol=None, vip_level="VIP0",
        maker_bps=Decimal("2"), taker_bps=Decimal("6"),
        effective_at=NOW, source="seed",
    )
    r = c.put("/api/fee-schedules", headers=_auth(), json=[fee.model_dump(mode="json")])
    assert r.status_code == 200
    r = c.get("/api/fee-schedules")  # global readable
    assert r.status_code == 200
    assert len(r.json()) == 1


def test_strategies_returns_mvp_three(client):
    c, _ = client
    r = c.get("/api/strategies", headers=_auth())
    assert r.status_code == 200
    keys = {s["key"] for s in r.json()}
    assert keys == {"ema_adx_trend", "vwap_rsi_meanrev", "fvg_ob"}


# ── backtests + stream ─────────────────────────────────────────────────────


def test_queue_backtest_returns_optimistic_flag(client):
    c, _ = client
    r = c.post(
        "/api/backtests",
        headers=_auth(),
        json={"strategy": "ema_adx_trend", "pair": "BTCUSDT", "timeframe": "5m", "bars": 1500},
    )
    assert r.status_code == 202
    assert r.json()["optimistic"] is True
    assert r.json()["status"] == "queued"


def test_stream_returns_sse_content_type(client):
    c, _ = client
    with c.stream("GET", "/api/stream", headers=_auth()) as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        # Consume one chunk so the stream completes.
        first = next(r.iter_bytes(), b"")
        assert b"ready" in first
