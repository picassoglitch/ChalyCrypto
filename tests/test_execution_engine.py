"""ExecutionEngine tests — the only path that writes an order to a venue.

Uses the REAL RiskEngine to produce an approved decision (so the executor is exercised
against a genuine TradeDecision, not a hand-rolled one) and a FakeConnector to capture
what would hit the exchange. No network, no real venue.

Covers the CLAUDE.md guarantees: only an approved semi_auto entry executes, full_auto and
paper never do, every write is idempotent, and any connector failure fails safe.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from chalybcrypto_connectors.base import (
    Balance,
    ConnectorError,
    ExchangeConnector,
    FundingInfo,
    OrderRequest,
    OrderResult,
    PositionInfo,
)
from chalybcrypto_engine.execution import (
    ExecutionEngine,
    ExecutionStatus,
    order_request_from_decision,
)
from chalybcrypto_engine.risk import InMemoryIdempotencyStore, RiskEngine
from chalybcrypto_shared import Mode, OrderType, Side

from tests.risk._helpers import (
    NOW,
    make_account,
    make_ev_inputs,
    make_profile,
    make_signal,
    make_stats,
)


# ── fake venue ─────────────────────────────────────────────────────────────


class FakeConnector(ExchangeConnector):
    """Records orders instead of sending them. Can be told to fail a given way."""

    exchange = "fake"

    def __init__(self, *, raise_error: Exception | None = None) -> None:
        self.placed: list[OrderRequest] = []
        self._raise = raise_error

    async def place_order(self, req: OrderRequest) -> OrderResult:
        if self._raise is not None:
            raise self._raise
        self.placed.append(req)
        return OrderResult(
            exchange_order_id=f"ex-{len(self.placed)}",
            client_id=req.idempotency_key,
            status="submitted",
            submitted_at=datetime.now(timezone.utc),
        )

    # unused abstract methods — the executor only calls place_order
    async def klines(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def order_book(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def funding(self, *a, **k) -> FundingInfo:  # pragma: no cover
        raise NotImplementedError

    async def balances(self, *a, **k) -> list[Balance]:  # pragma: no cover
        raise NotImplementedError

    async def positions(self, *a, **k) -> list[PositionInfo]:  # pragma: no cover
        raise NotImplementedError

    async def cancel_order(self, *a, **k) -> bool:  # pragma: no cover
        raise NotImplementedError


async def _approved_decision():
    """Run the real risk engine to get an approved semi_auto entry. Stats + fees are set
    generously so the EV gate clears with margin — this fixture is about execution, not
    about exercising the EV boundary (that's covered in tests/risk/test_ev.py)."""
    sig = make_signal()
    decision = await RiskEngine().authorize_new_entry(
        signal=sig,
        account=make_account(),
        risk_profile=make_profile(),
        ev_inputs=make_ev_inputs(
            taker_bps=Decimal("1"), maker_bps=Decimal("1"),
            spread_bps=Decimal("0"), slippage_bps=Decimal("0"),
            hold_hours=Decimal("0"), funding_rate=Decimal("0"),
        ),
        strategy_stats=make_stats(
            win_rate=Decimal("0.70"), avg_win_bps=Decimal("200"), avg_loss_bps=Decimal("30")
        ),
        idempotency_store=InMemoryIdempotencyStore(),
        mode=Mode.SEMI_AUTO,
        now=NOW,
    )
    assert decision.approved, f"fixture expected approval, got {decision.reason}"
    return sig, decision


# ── tests ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_approved_decision_places_market_order():
    sig, decision = await _approved_decision()
    req = order_request_from_decision(decision, pair=sig.pair, side=sig.side)
    conn = FakeConnector()

    report = await ExecutionEngine().open_position(
        req, connector=conn, idempotency_store=InMemoryIdempotencyStore(), mode=Mode.SEMI_AUTO
    )

    assert report.status is ExecutionStatus.FILLED
    assert report.placed
    assert report.exchange_order_id == "ex-1"
    assert len(conn.placed) == 1
    sent = conn.placed[0]
    assert sent.pair == sig.pair
    assert sent.side is Side.LONG
    assert sent.order_type is OrderType.MARKET
    assert sent.reduce_only is False
    assert sent.qty == decision.intended_qty
    assert sent.idempotency_key == decision.idempotency_key
    # stop-loss rides along with the entry so the position is never naked
    assert sent.stop_loss_price == decision.intended_stop_loss


@pytest.mark.asyncio
async def test_builder_refuses_unapproved_decision():
    sig = make_signal()
    rejected = await RiskEngine().authorize_new_entry(
        signal=sig,
        account=make_account(locked=True),  # account lock → reject
        risk_profile=make_profile(),
        ev_inputs=make_ev_inputs(),
        strategy_stats=make_stats(),
        idempotency_store=InMemoryIdempotencyStore(),
        mode=Mode.SEMI_AUTO,
        now=NOW,
    )
    assert not rejected.approved
    with pytest.raises(ValueError):
        order_request_from_decision(rejected, pair=sig.pair, side=sig.side)


@pytest.mark.asyncio
async def test_connector_error_fails_safe():
    sig, decision = await _approved_decision()
    req = order_request_from_decision(decision, pair=sig.pair, side=sig.side)
    conn = FakeConnector(raise_error=ConnectorError("bitunix 500"))

    report = await ExecutionEngine().open_position(
        req, connector=conn, idempotency_store=InMemoryIdempotencyStore(), mode=Mode.SEMI_AUTO
    )

    assert report.status is ExecutionStatus.FAILED
    assert "connector_error" in report.reason
    assert not report.placed
    assert conn.placed == []  # nothing recorded as sent


@pytest.mark.asyncio
async def test_unexpected_error_still_fails_safe():
    sig, decision = await _approved_decision()
    req = order_request_from_decision(decision, pair=sig.pair, side=sig.side)
    conn = FakeConnector(raise_error=ValueError("boom"))

    report = await ExecutionEngine().open_position(
        req, connector=conn, idempotency_store=InMemoryIdempotencyStore(), mode=Mode.SEMI_AUTO
    )
    assert report.status is ExecutionStatus.FAILED
    assert "unexpected_error" in report.reason


@pytest.mark.asyncio
async def test_duplicate_idempotency_sends_once():
    sig, decision = await _approved_decision()
    req = order_request_from_decision(decision, pair=sig.pair, side=sig.side)
    conn = FakeConnector()
    idem = InMemoryIdempotencyStore()
    eng = ExecutionEngine()

    first = await eng.open_position(req, connector=conn, idempotency_store=idem, mode=Mode.SEMI_AUTO)
    second = await eng.open_position(req, connector=conn, idempotency_store=idem, mode=Mode.SEMI_AUTO)

    assert first.status is ExecutionStatus.FILLED
    assert second.status is ExecutionStatus.DUPLICATE
    assert len(conn.placed) == 1  # rule 8: never double-sent


@pytest.mark.asyncio
async def test_full_auto_is_refused():
    sig, decision = await _approved_decision()
    req = order_request_from_decision(decision, pair=sig.pair, side=sig.side)
    conn = FakeConnector()

    report = await ExecutionEngine().open_position(
        req, connector=conn, idempotency_store=InMemoryIdempotencyStore(), mode=Mode.FULL_AUTO
    )
    assert report.status is ExecutionStatus.REJECTED
    assert report.reason == "full_auto_disabled_in_mvp"
    assert conn.placed == []


@pytest.mark.asyncio
async def test_paper_mode_never_reaches_venue():
    sig, decision = await _approved_decision()
    req = order_request_from_decision(decision, pair=sig.pair, side=sig.side)
    conn = FakeConnector()

    report = await ExecutionEngine().open_position(
        req, connector=conn, idempotency_store=InMemoryIdempotencyStore(), mode=Mode.PAPER
    )
    assert report.status is ExecutionStatus.REJECTED
    assert "mode_not_executable" in report.reason
    assert conn.placed == []


@pytest.mark.asyncio
async def test_close_position_is_reduce_only_opposite_side():
    conn = FakeConnector()
    report = await ExecutionEngine().close_position(
        pair="BTCUSDT",
        position_side=Side.LONG,
        qty=Decimal("0.5"),
        connector=conn,
        idempotency_store=InMemoryIdempotencyStore(),
        idempotency_key="close-1",
    )
    assert report.status is ExecutionStatus.FILLED
    assert len(conn.placed) == 1
    sent = conn.placed[0]
    assert sent.side is Side.SHORT  # closing a long sells
    assert sent.reduce_only is True
    assert sent.order_type is OrderType.REDUCE_ONLY_MARKET
