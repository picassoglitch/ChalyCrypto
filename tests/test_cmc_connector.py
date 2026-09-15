"""CMC connector tests against recorded fixtures (CI never hits live API)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from chalybcrypto_connectors import CmcConnector, ConnectorError


FIXTURES = Path(__file__).parent / "fixtures" / "cmc"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _make(handler: Callable[[httpx.Request], httpx.Response], *, with_key: bool = True) -> CmcConnector:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://pro-api.coinmarketcap.com", transport=transport)
    return CmcConnector(api_key="test-key" if with_key else None, client=client)


async def test_listings_latest_parses_fixture():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        return httpx.Response(200, json=_load("listings_latest.json"))

    c = _make(handler)
    try:
        rows = await c.listings_latest(limit=2)
    finally:
        await c.aclose()

    assert "/v1/cryptocurrency/listings/latest" in captured["url"]
    assert captured["headers"]["x-cmc_pro_api_key"] == "test-key"
    assert len(rows) == 2
    assert rows[0].symbol == "BTC"
    assert rows[0].rank == 1
    assert rows[0].price_usd == Decimal("60123.45")
    assert rows[0].market_cap_usd == Decimal("1180000000000")


async def test_quotes_latest_returns_keyed_dict():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_load("quotes_latest.json"))

    c = _make(handler)
    try:
        q = await c.quotes_latest(["BTC", "ETH"])
    finally:
        await c.aclose()

    assert set(q.keys()) == {"BTC", "ETH"}
    assert q["BTC"].price_usd == Decimal("60125.00")
    assert q["ETH"].percent_change_24h == Decimal("-1.1")


async def test_quotes_latest_empty_symbols_returns_empty_dict_without_calling():
    called = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"status": {"error_code": 0}, "data": {}})

    c = _make(handler)
    try:
        out = await c.quotes_latest([])
    finally:
        await c.aclose()

    assert out == {}
    assert called["n"] == 0  # no request fired


async def test_missing_api_key_raises_connector_error():
    c = _make(lambda r: httpx.Response(200, json={}), with_key=False)
    try:
        with pytest.raises(ConnectorError, match="api_key required"):
            await c.listings_latest()
    finally:
        await c.aclose()


async def test_cmc_error_code_raises_connector_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"status": {"error_code": 1006, "error_message": "API key invalid"}, "data": []},
        )

    c = _make(handler)
    try:
        with pytest.raises(ConnectorError) as exc:
            await c.listings_latest()
        assert exc.value.code == "1006"
    finally:
        await c.aclose()


async def test_http_error_raises_connector_error():
    c = _make(lambda r: httpx.Response(429, text="rate limited"))
    try:
        with pytest.raises(ConnectorError) as exc:
            await c.listings_latest()
        assert exc.value.status == 429
    finally:
        await c.aclose()
