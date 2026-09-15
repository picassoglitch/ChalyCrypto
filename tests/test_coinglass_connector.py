"""Coinglass connector tests against recorded fixtures (CI never hits live API)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from chalybcrypto_connectors import CoinglassConnector, ConnectorError


FIXTURES = Path(__file__).parent / "fixtures" / "coinglass"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _make(handler: Callable[[httpx.Request], httpx.Response], *, with_key: bool = True) -> CoinglassConnector:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://open-api-v4.coinglass.com", transport=transport)
    return CoinglassConnector(api_key="test-key" if with_key else None, client=client)


async def test_open_interest_history_parses_and_sends_api_key():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json=_load("oi_history.json"))

    c = _make(handler)
    try:
        rows = await c.open_interest_history("BTC", interval="1h")
    finally:
        await c.aclose()

    assert "/api/futures/openInterest/ohlc-history" in captured["url"]
    assert captured["headers"]["cg-api-key"] == "test-key"
    assert "symbol=BTC" in captured["url"]
    assert len(rows) == 2
    assert rows[0].close == Decimal("12.7")
    assert rows[0].taken_at == datetime.fromtimestamp(1780728000, tz=timezone.utc)


async def test_funding_oi_weighted_history_parses():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_load("funding_weighted.json"))

    c = _make(handler)
    try:
        rows = await c.funding_oi_weighted_history("BTC")
    finally:
        await c.aclose()

    assert len(rows) == 2
    assert rows[1].weighted_rate == Decimal("0.00012")


async def test_liquidations_aggregated_history_parses():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_load("liquidations.json"))

    c = _make(handler)
    try:
        rows = await c.liquidations_aggregated_history("BTC")
    finally:
        await c.aclose()

    assert len(rows) == 2
    assert rows[0].long_liq_usd == Decimal("1250000")
    assert rows[1].short_liq_usd == Decimal("1820000")


async def test_missing_api_key_raises():
    c = _make(lambda r: httpx.Response(200, json={"code": "0", "data": []}), with_key=False)
    try:
        with pytest.raises(ConnectorError, match="api_key required"):
            await c.open_interest_history("BTC")
    finally:
        await c.aclose()


async def test_coinglass_nonzero_code_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": "10001", "msg": "rate limited", "data": None})

    c = _make(handler)
    try:
        with pytest.raises(ConnectorError) as exc:
            await c.open_interest_history("BTC")
        assert exc.value.code == "10001"
    finally:
        await c.aclose()


async def test_http_error_raises():
    c = _make(lambda r: httpx.Response(503, text="upstream gone"))
    try:
        with pytest.raises(ConnectorError) as exc:
            await c.open_interest_history("BTC")
        assert exc.value.status == 503
    finally:
        await c.aclose()
