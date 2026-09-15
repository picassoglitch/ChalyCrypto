"""Execution coordinator tests — the approve→order glue.

Drives the full loop with an InMemoryStore and an injected fake venue, so no real keys,
no vault, no network. Proves: a live approve places the order and records trade + order +
audit + resolves the approval; non-live modes never reach the venue; missing connection
or a connector failure leaves the approval pending and audited.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import pytest

from chalybcrypto_api.execution_coordinator import handle_approval_approve
from chalybcrypto_api.store import InMemoryStore
from chalybcrypto_engine.risk import InMemoryIdempotencyStore
from chalybcrypto_connectors.base import (
    ConnectorError,
    ExchangeConnector,
    OrderRequest,
    OrderResult,
)
from chalybcrypto_shared import (
    MarginType,
    Mode,
    Side,
    Signal,
    TradeDecision,
    dedup_hash,
)

NOW = datetime(2026, 6, 6, tzinfo=timezone.utc)
USER = UUID("11111111-1111-1111-1111-111111111111")


class FakeConnector(ExchangeConnector):
    exchange = "bitunix"

    def __init__(self, *, raise_error: Exception | None = None) -> None:
        self.placed: list[OrderRequest] = []
        self.closed = False
        self._raise = raise_error

    async def place_order(self, req: OrderRequest) -> OrderResult:
        if self._raise is not None:
            raise self._raise
        self.placed.append(req)
        return OrderResult(
            exchange_order_id="ex-1", client_id=req.idempotency_key,
            status="submitted", submitted_at=NOW,
        )

    async def aclose(self) -> None:
        self.closed = True

    async def klines(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def order_book(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def funding(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def balances(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def positions(self, *a, **k):  # pragma: no cover
        raise NotImplementedError

    async def cancel_order(self, *a, **k):  # pragma: no cover
        raise NotImplementedError


def _signal() -> Signal:
    return Signal(
        pair="BTCUSDT", side=Side.LONG, strategy="ema_adx_trend",
        entry=Decimal("60000"), stop_loss=Decimal("59700"),
        take_profits=[Decimal("60900")], leverage=Decimal("10"),
        margin_type=MarginType.ISOLATED, timeframe="5m",
        thesis_tags=[], source="scanner",
        dedup_hash=dedup_hash("BTCUSDT", "long"), created_at=NOW,
    )


async def _queue_approval(store: InMemoryStore) -> dict:
    sig = _signal()
    td = TradeDecision(
        signal_id=sig.id, mode=Mode.SEMI_AUTO, approved=True, reason="ok",
        intended_qty=Decimal("0.01"), intended_leverage=Decimal("10"),
        intended_stop_loss=Decimal("59700"), intended_take_profits=[Decimal("60900")],
        idempotency_key=dedup_hash("exec-key"), decided_at=NOW,
    )
    return await store.add_approval(user_id=USER, signal=sig, decision=td)


async def _go_live(store: InMemoryStore, *, with_connection: bool = True) -> None:
    await store.unlock_live_for_test(user_id=USER)
    await store.set_mode(user_id=USER, mode=Mode.SEMI_AUTO)
    if with_connection:
        await store.add_exchange_connection(
            user_id=USER, exchange="bitunix", api_key_enc=b"k", api_secret_enc=b"s"
        )


@pytest.mark.asyncio
async def test_live_approve_places_order_and_records_everything():
    store = InMemoryStore()
    approval = await _queue_approval(store)
    await _go_live(store)
    conn = FakeConnector()

    result = await handle_approval_approve(
        store=store, user_id=USER, approval=approval,
        connector_factory=lambda exchange, enc: conn,
        idempotency_store=InMemoryIdempotencyStore(),
    )

    # order placed on the venue with the approved parameters
    assert result["executed"] is True
    assert result["status"] == "filled"
    assert result["exchange_order_id"] == "ex-1"
    assert len(conn.placed) == 1
    sent = conn.placed[0]
    assert sent.pair == "BTCUSDT"
    assert sent.side is Side.LONG
    assert sent.reduce_only is False
    assert sent.qty == Decimal("0.01")
    assert sent.idempotency_key == approval["idempotency_key"]
    assert conn.closed is True  # connector always released

    # persisted: trade, order, audit; approval resolved
    trades = await store.list_trades(user_id=USER)
    assert len(trades) == 1 and trades[0]["status"] == "open" and trades[0]["pair"] == "BTCUSDT"
    audits = await store.list_audit_logs(user_id=USER)
    assert any(a["action"] == "open_order" for a in audits)
    resolved = await store.get_approval(user_id=USER, approval_id=approval["id"])
    assert resolved["status"] == "approve"


@pytest.mark.asyncio
async def test_paper_mode_records_approval_but_never_executes():
    store = InMemoryStore()
    approval = await _queue_approval(store)
    # default mode = paper, not live-unlocked → must NOT touch a venue

    def _factory(exchange, enc):  # pragma: no cover - must never be called
        raise AssertionError("connector built in paper mode")

    result = await handle_approval_approve(
        store=store, user_id=USER, approval=approval, connector_factory=_factory
    )
    assert result["status"] == "approve"  # resolved, backward-compatible shape
    assert await store.list_trades(user_id=USER) == []


@pytest.mark.asyncio
async def test_live_approve_blocked_without_connection():
    store = InMemoryStore()
    approval = await _queue_approval(store)
    await _go_live(store, with_connection=False)

    result = await handle_approval_approve(
        store=store, user_id=USER, approval=approval,
        connector_factory=lambda e, c: FakeConnector(),
        idempotency_store=InMemoryIdempotencyStore(),
    )
    assert result["executed"] is False
    assert result["reason"] == "no_bitunix_connection"
    # approval stays pending so the operator can add keys and retry
    still = await store.get_approval(user_id=USER, approval_id=approval["id"])
    assert still["status"] == "pending"
    assert any(a["action"] == "open_order_blocked" for a in await store.list_audit_logs(user_id=USER))


@pytest.mark.asyncio
async def test_connector_failure_leaves_approval_pending():
    store = InMemoryStore()
    approval = await _queue_approval(store)
    await _go_live(store)
    conn = FakeConnector(raise_error=ConnectorError("bitunix 500"))

    result = await handle_approval_approve(
        store=store, user_id=USER, approval=approval,
        connector_factory=lambda e, c: conn,
        idempotency_store=InMemoryIdempotencyStore(),
    )
    assert result["executed"] is False
    assert result["status"] == "failed"
    assert await store.list_trades(user_id=USER) == []  # nothing recorded as a trade
    still = await store.get_approval(user_id=USER, approval_id=approval["id"])
    assert still["status"] == "pending"
    assert conn.closed is True
    assert any(a["action"] == "open_order_failed" for a in await store.list_audit_logs(user_id=USER))
