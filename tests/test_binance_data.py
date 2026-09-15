"""Binance data-only kline fetcher tests against recorded fixtures.

ARCHITECTURE §0.3: Binance is permitted only as a public historical-kline source for
backtests. These tests also verify the class deliberately exposes nothing else.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from chalybcrypto_connectors import BinanceDataConnector, ConnectorError


FIXTURES = Path(__file__).parent / "fixtures" / "binance"


def _make(handler: Callable[[httpx.Request], httpx.Response]) -> BinanceDataConnector:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://fapi.binance.com", transport=transport)
    return BinanceDataConnector(client=client)


async def test_klines_parses_official_array_shape():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=json.loads((FIXTURES / "klines.json").read_text()))

    c = _make(handler)
    try:
        rows = await c.klines("BTCUSDT", "1m", limit=2)
    finally:
        await c.aclose()

    assert "/fapi/v1/klines" in captured["url"]
    assert "symbol=BTCUSDT" in captured["url"]
    assert "interval=1m" in captured["url"]
    assert len(rows) == 2
    assert rows[0].open_time == datetime.fromtimestamp(1764979200, tz=timezone.utc)
    assert rows[0].open == Decimal("60000.00")
    assert rows[0].close == Decimal("60100.00")
    assert rows[0].high == Decimal("60150.00")
    assert rows[0].low == Decimal("59950.00")
    assert rows[0].volume == Decimal("12.345")
    assert rows[1].close == Decimal("60175.50")


async def test_klines_limit_clamped_to_max_1500():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json=[])

    c = _make(handler)
    try:
        await c.klines("BTCUSDT", "1m", limit=99999)
    finally:
        await c.aclose()

    assert "limit=1500" in captured["url"]


async def test_klines_rejects_bad_interval():
    c = _make(lambda r: httpx.Response(500))
    try:
        with pytest.raises(ConnectorError, match="unknown interval"):
            await c.klines("BTCUSDT", "7m")
    finally:
        await c.aclose()


async def test_http_error_status_raises_connector_error():
    c = _make(lambda r: httpx.Response(429, text="rate limited"))
    try:
        with pytest.raises(ConnectorError) as exc:
            await c.klines("BTCUSDT", "1m")
        assert exc.value.status == 429
    finally:
        await c.aclose()


def test_class_does_not_expose_trading_methods():
    """ARCHITECTURE §0.3: Binance is data-only. No order/balance methods may exist."""
    forbidden = {
        "place_order",
        "cancel_order",
        "balances",
        "positions",
        "order_book",  # third-party books cannot gate fills (CLAUDE.md)
    }
    public_methods = {name for name in dir(BinanceDataConnector) if not name.startswith("_")}
    leaked = forbidden & public_methods
    assert not leaked, f"Binance data class must not expose: {leaked}"
