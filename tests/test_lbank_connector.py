"""LBank public connector tests against recorded fixtures.

CI never hits the live API (CLAUDE.md). httpx.MockTransport intercepts every call.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from chalybcrypto_connectors import ConnectorError, LBankPublicConnector


FIXTURES = Path(__file__).parent / "fixtures" / "lbank"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _make(handler: Callable[[httpx.Request], httpx.Response]) -> LBankPublicConnector:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://lbkperp.lbank.com", transport=transport)
    return LBankPublicConnector(client=client)


async def test_order_book_parses_and_marks_native():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=_load("market_order.json"))

    c = _make(handler)
    try:
        ob = await c.order_book("BTCUSDT", depth=20)
    finally:
        await c.aclose()

    assert "/cfd/openApi/v1/pub/marketOrder" in captured["url"]
    assert "symbol=BTCUSDT" in captured["url"]
    assert "depth=20" in captured["url"]
    assert ob.exchange == "lbank"
    assert ob.is_native is True  # CLAUDE.md §0.3: native books gate fills
    assert len(ob.bids) == 3
    assert ob.bids[0].price == Decimal("60099.5")
    assert ob.asks[0].size == Decimal("0.5")


async def test_market_data_returns_tickers_with_funding_rate():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_load("market_data.json"))

    c = _make(handler)
    try:
        tickers = await c.market_data("SwapU")
    finally:
        await c.aclose()

    assert len(tickers) == 2
    by_sym = {t.symbol: t for t in tickers}
    btc = by_sym["BTCUSDT"]
    assert btc.last_price == Decimal("60100.5")
    assert btc.mark_price == Decimal("60101.2")
    assert btc.funding_rate == Decimal("0.0001")  # from prePositionFeeRate
    eth = by_sym["ETHUSDT"]
    assert eth.funding_rate == Decimal("0.00005")


async def test_error_code_nonzero_raises_connector_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": {}, "error_code": 4001, "msg": "rate limited", "success": False}
        )

    c = _make(handler)
    try:
        with pytest.raises(ConnectorError) as exc:
            await c.order_book("BTCUSDT")
        assert exc.value.code == "4001"
    finally:
        await c.aclose()


async def test_http_error_status_raises_connector_error():
    c = _make(lambda r: httpx.Response(503, text="upstream gone"))
    try:
        with pytest.raises(ConnectorError) as exc:
            await c.order_book("BTCUSDT")
        assert exc.value.status == 503
    finally:
        await c.aclose()


def test_public_class_does_not_expose_trading_methods():
    """The narrow surface is deliberate — no klines, no trading. See connector docstring."""
    forbidden = {"place_order", "cancel_order", "balances", "positions", "klines"}
    public_methods = {name for name in dir(LBankPublicConnector) if not name.startswith("_")}
    leaked = forbidden & public_methods
    assert not leaked, f"LBankPublicConnector must not expose: {leaked}"
