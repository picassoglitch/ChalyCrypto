"""End-to-end: scanner writes signals through PgStore, API reads them back."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from chalybcrypto_api.deps import set_store_for_tests
from chalybcrypto_api.main import app
from chalybcrypto_api.pg_store import PgStore
from chalybcrypto_engine.risk import StrategyStats
from chalybcrypto_engine.strategy import EmaAdxTrendParams, EmaAdxTrendStrategy
from chalybcrypto_worker import scan_once
from chalybcrypto_shared import Kline, Mode

from tests.strategy._helpers import flat_series, pullback_then_resume_uptrend


NOW = datetime(2026, 6, 6, 16, 0, tzinfo=timezone.utc)


@dataclass
class _FakeFunding:
    mark_price: Decimal = Decimal("60000")
    funding_rate: Decimal = Decimal("0.0001")


class _FakeSource:
    def __init__(self, series: list[Kline]) -> None:
        self._series = series

    async def klines(self, pair, interval, *, limit=100):
        return self._series[-limit:]

    async def funding(self, pair):
        return _FakeFunding(mark_price=self._series[-1].close)


@pytest.fixture
def pg_store(db_dsn) -> PgStore:
    return PgStore(db_dsn)


@pytest.fixture
def client(pg_store):
    set_store_for_tests(pg_store)
    with TestClient(app) as c:
        yield c


async def test_scanner_writes_through_pgstore_visible_via_api(client, pg_store):
    user = UUID("22222222-2222-2222-2222-222222222222")
    src = _FakeSource(pullback_then_resume_uptrend())

    # Scanner runs end-to-end with persistence enabled.
    result = await scan_once(
        src,
        "BTCUSDT",
        interval="5m",
        bars=300,
        strategies=[(EmaAdxTrendStrategy(), EmaAdxTrendParams(adx_threshold=Decimal("15")))],
        now=NOW,
        store=pg_store,
        user_id=user,
    )
    assert any(o.fired for o in result.outcomes)

    # API now sees what the scanner wrote.
    r = client.get("/api/signals", headers={"X-User-Id": str(user)})
    assert r.status_code == 200
    rows = r.json()
    # parsed signal + validated decision = 2 rows
    statuses = sorted(r["status"] for r in rows)
    assert "parsed" in statuses
    assert ("approved" in statuses) or ("rejected" in statuses)


async def test_scanner_no_writes_when_store_is_none(client, pg_store):
    """Scanner used without a store should NOT persist."""
    user = UUID("33333333-3333-3333-3333-333333333333")
    src = _FakeSource(flat_series(220, price=100))
    await scan_once(src, "BTCUSDT", interval="5m", bars=220, now=NOW)
    r = client.get("/api/signals", headers={"X-User-Id": str(user)})
    assert r.status_code == 200
    assert r.json() == []


# ── semi-auto: scanner-approved signal queues for human review ─────────────


async def test_semi_auto_approved_signal_lands_in_approvals_queue(client, pg_store):
    """Full loop: SEMI_AUTO mode + approved decision + persistent store →
    /api/approvals returns the queued row; POST /decision flips status."""
    user = UUID("44444444-4444-4444-4444-444444444444")
    src = _FakeSource(pullback_then_resume_uptrend())

    # EV gate needs validated stats for live/semi-auto modes.
    stats = StrategyStats(
        strategy="ema_adx_trend",
        sample_size=100,
        win_rate=Decimal("0.55"),
        avg_win_bps=Decimal("80"),
        avg_loss_bps=Decimal("40"),
    )

    result = await scan_once(
        src, "BTCUSDT", interval="5m", bars=300,
        strategies=[(EmaAdxTrendStrategy(), EmaAdxTrendParams(adx_threshold=Decimal("15")))],
        mode=Mode.SEMI_AUTO,
        strategy_stats={"ema_adx_trend": stats},
        now=NOW, store=pg_store, user_id=user,
    )
    fired_and_approved = [
        o for o in result.outcomes if o.fired and o.decision and o.decision.approved
    ]
    assert fired_and_approved, "expected at least one approved decision in SEMI_AUTO"

    # Queue should now contain the approval.
    r = client.get("/api/approvals", headers={"X-User-Id": str(user)})
    assert r.status_code == 200
    approvals = r.json()
    assert len(approvals) == 1
    assert approvals[0]["pair"] == "BTCUSDT"
    assert approvals[0]["status"] == "pending"
    assert approvals[0]["side"] == "long"

    # Resolve it via the API.
    approval_id = approvals[0]["id"]
    r = client.post(
        f"/api/approvals/{approval_id}/decision",
        headers={"X-User-Id": str(user)},
        json={"action": "approve"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "approved"

    # Queue is empty after resolution.
    r = client.get("/api/approvals", headers={"X-User-Id": str(user)})
    assert r.json() == []


async def test_paper_mode_does_not_queue_approvals(client, pg_store):
    """In PAPER mode, the scanner persists signals + decisions but DOES NOT create
    approval rows — fills are auto-simulated. This guards against an accidental
    semi-auto-only branch firing in paper too."""
    user = UUID("55555555-5555-5555-5555-555555555555")
    src = _FakeSource(pullback_then_resume_uptrend())

    await scan_once(
        src, "BTCUSDT", interval="5m", bars=300,
        strategies=[(EmaAdxTrendStrategy(), EmaAdxTrendParams(adx_threshold=Decimal("15")))],
        mode=Mode.PAPER,   # default, but explicit
        now=NOW, store=pg_store, user_id=user,
    )

    r = client.get("/api/approvals", headers={"X-User-Id": str(user)})
    assert r.status_code == 200
    assert r.json() == []  # no approval queued in paper mode


async def test_semi_auto_rejected_signal_does_not_queue(client, pg_store):
    """RejectED signals must not enter the approval queue — only approved ones do."""
    user = UUID("66666666-6666-6666-6666-666666666666")
    src = _FakeSource(pullback_then_resume_uptrend())

    # No stats → EV gate rejects for SEMI_AUTO with EV_STATS_UNKNOWN.
    result = await scan_once(
        src, "BTCUSDT", interval="5m", bars=300,
        strategies=[(EmaAdxTrendStrategy(), EmaAdxTrendParams(adx_threshold=Decimal("15")))],
        mode=Mode.SEMI_AUTO,
        strategy_stats=None,
        now=NOW, store=pg_store, user_id=user,
    )
    rejected = [o for o in result.outcomes if o.fired and o.decision and not o.decision.approved]
    assert rejected, "expected a rejection on missing stats"

    r = client.get("/api/approvals", headers={"X-User-Id": str(user)})
    assert r.json() == []  # nothing queued — only approved entries go to humans
