"""Bitunix REST connector tests against recorded fixtures.

httpx.MockTransport intercepts every call — CI never hits live APIs (CLAUDE.md).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from chalybcrypto_connectors import ConnectorError
from chalybcrypto_connectors.base import OrderRequest, PositionSide
from chalybcrypto_connectors.bitunix import BitunixConnector
from chalybcrypto_shared import MarginType, OrderType, Side


FIXTURES = Path(__file__).parent / "fixtures" / "bitunix"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _make_connector(handler: Callable[[httpx.Request], httpx.Response], *, authed: bool = False) -> BitunixConnector:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://fapi.bitunix.com", transport=transport)
    return BitunixConnector(
        api_key="test-key" if authed else None,
        api_secret="test-secret" if authed else None,
        client=client,
    )


@pytest.mark.asyncio
async def test_klines_parses_fixture_into_shared_model():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json=_load("kline.json"))

    c = _make_connector(handler)
    try:
        rows = await c.klines("BTCUSDT", "1m", limit=2)
    finally:
        await c.aclose()

    assert "/api/v1/futures/market/kline" in captured["url"]
    assert "symbol=BTCUSDT" in captured["url"]
    assert "interval=1m" in captured["url"]
    # No auth headers on public endpoints.
    assert "sign" not in captured["headers"]
    assert len(rows) == 2
    assert rows[0].open == Decimal("60000")
    assert rows[0].close == Decimal("60050")
    assert rows[0].volume == Decimal("0.02055")
    assert rows[0].open_time == datetime.fromtimestamp(1764979200, tz=timezone.utc)


@pytest.mark.asyncio
async def test_klines_rejects_bad_interval():
    c = _make_connector(lambda r: httpx.Response(500))
    try:
        with pytest.raises(ConnectorError, match="unknown interval"):
            await c.klines("BTCUSDT", "1ms")
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_order_book_parses_and_marks_native():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_load("depth.json"))

    c = _make_connector(handler)
    try:
        ob = await c.order_book("BTCUSDT", limit=5)
    finally:
        await c.aclose()

    assert ob.exchange == "bitunix"
    assert ob.is_native is True  # CLAUDE.md: fill-gating book must be native
    assert len(ob.bids) == 3
    assert ob.bids[0].price == Decimal("60099.9")
    assert ob.asks[0].size == Decimal("0.5")


@pytest.mark.asyncio
async def test_funding_parses_first_element():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_load("funding_rate.json"))

    c = _make_connector(handler)
    try:
        f = await c.funding("BTCUSDT")
    finally:
        await c.aclose()

    assert f.pair == "BTCUSDT"
    assert f.funding_rate == Decimal("0.0001")
    assert f.funding_interval_hours == 8
    assert f.mark_price == Decimal("60100")
    assert f.next_funding_time == datetime.fromtimestamp(1764979200, tz=timezone.utc)


@pytest.mark.asyncio
async def test_non_zero_code_raises_connector_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 10001, "msg": "rate limited", "data": None})

    c = _make_connector(handler)
    try:
        with pytest.raises(ConnectorError) as exc:
            await c.funding("BTCUSDT")
        assert exc.value.code == "10001"
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_balances_requires_keys_and_signs():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_load("account.json"))

    # Without keys: should raise.
    c_noauth = _make_connector(lambda r: httpx.Response(200, json={}))
    try:
        with pytest.raises(ConnectorError, match="requires api_key"):
            await c_noauth.balances("USDT")
    finally:
        await c_noauth.aclose()

    c = _make_connector(handler, authed=True)
    try:
        bals = await c.balances("USDT")
    finally:
        await c.aclose()

    assert captured["headers"]["api-key"] == "test-key"
    assert len(captured["headers"]["sign"]) == 64
    assert captured["headers"]["nonce"]
    assert captured["headers"]["timestamp"]
    assert "marginCoin=USDT" in captured["url"]
    assert len(bals) == 1
    assert bals[0].available == Decimal("1000.50")
    assert bals[0].cross_unrealized_pnl == Decimal("2.30")


@pytest.mark.asyncio
async def test_positions_normalizes_side_and_drops_zero_liq():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_load("positions.json"))

    c = _make_connector(handler, authed=True)
    try:
        positions = await c.positions()
    finally:
        await c.aclose()

    by_pair = {p.pair: p for p in positions}
    assert by_pair["BTCUSDT"].side == PositionSide.LONG
    assert by_pair["BTCUSDT"].liquidation_price == Decimal("54000")
    assert by_pair["BTCUSDT"].margin_type == MarginType.ISOLATED
    # ETHUSDT had liqPrice = 0 → treat as "no risk yet", surface None.
    assert by_pair["ETHUSDT"].side == PositionSide.SHORT
    assert by_pair["ETHUSDT"].liquidation_price is None
    assert by_pair["ETHUSDT"].margin_type == MarginType.CROSS


@pytest.mark.asyncio
async def test_place_order_forwards_idempotency_key_as_client_id():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_load("place_order.json"))

    c = _make_connector(handler, authed=True)
    try:
        result = await c.place_order(
            OrderRequest(
                pair="BTCUSDT",
                side=Side.LONG,
                order_type=OrderType.MARKET,
                qty=Decimal("0.01"),
                reduce_only=False,
                idempotency_key="dedup-abc123",
            )
        )
    finally:
        await c.aclose()

    body = captured["body"]
    assert body["symbol"] == "BTCUSDT"
    assert body["side"] == "BUY"
    assert body["orderType"] == "MARKET"
    assert body["qty"] == "0.01"
    assert body["clientId"] == "dedup-abc123"
    assert "reduceOnly" not in body  # only set when requested
    assert result.exchange_order_id == "ord-987654321"
    assert result.client_id == "dedup-abc123"


@pytest.mark.asyncio
async def test_place_order_marks_reduce_only_when_requested():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_load("place_order.json"))

    c = _make_connector(handler, authed=True)
    try:
        await c.place_order(
            OrderRequest(
                pair="BTCUSDT",
                side=Side.SHORT,
                order_type=OrderType.REDUCE_ONLY_MARKET,
                qty=Decimal("0.005"),
                idempotency_key="exit-1",
            )
        )
    finally:
        await c.aclose()

    assert captured["body"]["reduceOnly"] is True
    assert captured["body"]["side"] == "SELL"


@pytest.mark.asyncio
async def test_place_order_limit_requires_price():
    c = _make_connector(lambda r: httpx.Response(200, json=_load("place_order.json")), authed=True)
    try:
        with pytest.raises(ConnectorError, match="price required"):
            await c.place_order(
                OrderRequest(
                    pair="BTCUSDT",
                    side=Side.LONG,
                    order_type=OrderType.LIMIT,
                    qty=Decimal("0.01"),
                    idempotency_key="k",
                )
            )
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_cancel_order_posts_canonical_body():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_load("cancel_orders.json"))

    c = _make_connector(handler, authed=True)
    try:
        ok = await c.cancel_order("BTCUSDT", order_id="ord-987654321")
    finally:
        await c.aclose()

    assert ok is True
    assert captured["body"] == {"symbol": "BTCUSDT", "orderList": [{"orderId": "ord-987654321"}]}


@pytest.mark.asyncio
async def test_cancel_order_requires_id_or_client_id():
    c = _make_connector(lambda r: httpx.Response(200, json=_load("cancel_orders.json")), authed=True)
    try:
        with pytest.raises(ConnectorError, match="need order_id or client_id"):
            await c.cancel_order("BTCUSDT")
    finally:
        await c.aclose()


@pytest.mark.asyncio
async def test_http_error_status_raises_connector_error():
    c = _make_connector(lambda r: httpx.Response(503, text="upstream gone"))
    try:
        with pytest.raises(ConnectorError) as exc:
            await c.funding("BTCUSDT")
        assert exc.value.status == 503
    finally:
        await c.aclose()
