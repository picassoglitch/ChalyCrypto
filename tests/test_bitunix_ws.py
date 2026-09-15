"""Bitunix WS transport tests.

Two layers:
  * Pure encoders — verify the wire format matches docs without any network.
  * Round-trip — connect to a local websockets server that mimics the Bitunix
    envelope shape, subscribe, receive a message, send ping, get pong. CI never
    hits the live Bitunix WS.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import pytest
import websockets
from websockets.asyncio.server import serve

from chalybcrypto_connectors.bitunix.ws import (
    BitunixDepthBooksStream,
    BitunixPublicWS,
    Channel,
    decode_depth_books,
    encode_ping,
    encode_subscribe,
    encode_unsubscribe,
    is_pong,
)


# ─── pure encoder tests ──────────────────────────────────────────────────


def test_encode_subscribe_matches_docs_shape():
    payload = json.loads(
        encode_subscribe([Channel(ch="depth_books", symbol="BTCUSDT")])
    )
    assert payload == {
        "op": "subscribe",
        "args": [{"ch": "depth_books", "symbol": "BTCUSDT"}],
    }


def test_encode_subscribe_supports_multiple_channels():
    payload = json.loads(
        encode_subscribe(
            [Channel("tickers", "BTCUSDT"), Channel("tickers", "ETHUSDT")]
        )
    )
    assert payload["op"] == "subscribe"
    assert len(payload["args"]) == 2


def test_encode_unsubscribe_matches_subscribe_shape():
    payload = json.loads(encode_unsubscribe([Channel("tickers", "BTCUSDT")]))
    assert payload["op"] == "unsubscribe"
    assert payload["args"] == [{"ch": "tickers", "symbol": "BTCUSDT"}]


def test_encode_ping_includes_op_and_ts():
    payload = json.loads(encode_ping(ts_ms=1764979200000))
    assert payload == {"op": "ping", "ts": 1764979200000}


def test_is_pong_detects_only_pong_op():
    assert is_pong({"op": "pong", "ts": 1}) is True
    assert is_pong({"op": "ping", "ts": 1}) is False
    assert is_pong({"ch": "tickers", "ts": 1, "data": []}) is False


# ─── round-trip against a local mock server ───────────────────────────────


@pytest.fixture
async def mock_ws_server():
    """In-process WS server mimicking Bitunix futures protocol. Yields (url, state)."""

    state: dict[str, Any] = {
        "subscribed": [],
        "unsubscribed": [],
        "pings_received": 0,
    }

    async def handler(ws):
        async for raw in ws:
            msg = json.loads(raw)
            op = msg.get("op")
            if op == "subscribe":
                state["subscribed"].extend(msg["args"])
                for arg in msg["args"]:
                    # echo a fake "tickers" envelope back so the client sees real data flow
                    if arg["ch"] == "tickers":
                        await ws.send(
                            json.dumps(
                                {
                                    "ch": "tickers",
                                    "ts": 1764979200000,
                                    "data": [{"s": arg["symbol"], "la": "60000"}],
                                }
                            )
                        )
            elif op == "unsubscribe":
                state["unsubscribed"].extend(msg["args"])
            elif op == "ping":
                state["pings_received"] += 1
                await ws.send(json.dumps({"op": "pong", "ts": msg["ts"]}))

    server = await serve(handler, "127.0.0.1", 0)
    sock = next(iter(server.sockets))
    port = sock.getsockname()[1]
    url = f"ws://127.0.0.1:{port}/"
    try:
        yield url, state
    finally:
        server.close()
        await server.wait_closed()


async def test_subscribe_and_receive_envelope(mock_ws_server):
    url, state = mock_ws_server

    async with BitunixPublicWS(url=url, heartbeat_seconds=999) as ws:
        await ws.subscribe(Channel("tickers", "BTCUSDT"))

        first = await asyncio.wait_for(ws.messages().__anext__(), timeout=3.0)

    assert first["ch"] == "tickers"
    assert first["data"][0]["s"] == "BTCUSDT"
    assert state["subscribed"] == [{"ch": "tickers", "symbol": "BTCUSDT"}]


async def test_pong_is_consumed_internally(mock_ws_server):
    url, state = mock_ws_server

    # heartbeat every 0.2s so we trigger a ping/pong inside the test window
    async with BitunixPublicWS(url=url, heartbeat_seconds=0.2) as ws:
        await ws.subscribe(Channel("tickers", "BTCUSDT"))
        # consume the initial tickers echo so the iterator is active
        first = await asyncio.wait_for(ws.messages().__anext__(), timeout=3.0)
        assert first["ch"] == "tickers"
        # wait for a heartbeat cycle to happen
        await asyncio.sleep(0.5)

    assert state["pings_received"] >= 1


async def test_unsubscribe_clears_local_state(mock_ws_server):
    url, state = mock_ws_server
    ch = Channel("tickers", "BTCUSDT")

    async with BitunixPublicWS(url=url, heartbeat_seconds=999) as ws:
        await ws.subscribe(ch)
        await asyncio.sleep(0.05)  # let server register the sub
        await ws.unsubscribe(ch)
        await asyncio.sleep(0.05)

    assert state["unsubscribed"] == [{"ch": "tickers", "symbol": "BTCUSDT"}]


async def test_subscribe_caps_at_300():
    """Local-only check — does not connect."""
    ws = BitunixPublicWS(url="ws://localhost:1")
    # bypass connect by lying that we have a ws so subscribe() reaches the cap check
    ws._ws = object()  # type: ignore[assignment]
    too_many = [Channel("tickers", f"S{i}") for i in range(301)]
    with pytest.raises(RuntimeError, match="subscription cap"):
        await ws.subscribe(*too_many)


# ─── depth_books decoder (shape verified from live capture 2026-06-06) ────


def test_decode_depth_books_parses_recorded_envelope():
    import json
    from pathlib import Path

    env = json.loads(
        (Path(__file__).parent / "fixtures" / "bitunix" / "ws_depth_books.json").read_text()
    )
    snap = decode_depth_books(env)
    assert snap is not None
    assert snap.exchange == "bitunix"
    assert snap.pair == "BTCUSDT"
    assert snap.is_native is True  # CLAUDE.md §0.3
    assert len(snap.asks) == 3
    assert len(snap.bids) == 3
    # Bitunix delivers asks ascending (best ask first)
    assert snap.asks[0].price == Decimal("60959.4")
    # ...and bids descending (best bid first)
    assert snap.bids[0].price == Decimal("60959.3")
    # Spread check
    assert snap.asks[0].price > snap.bids[0].price


def test_decode_depth_books_returns_none_for_other_channels():
    assert decode_depth_books({"ch": "tickers", "data": []}) is None
    assert decode_depth_books({"op": "connect", "data": {"result": True}}) is None
    assert decode_depth_books({}) is None


def test_decode_depth_books_returns_none_for_malformed_data():
    assert decode_depth_books({"ch": "depth_books", "data": "not a dict"}) is None
    assert decode_depth_books({"ch": "depth_books", "data": {"a": "x"}}) is None


def test_decode_depth_books_uses_ts_when_present():
    snap = decode_depth_books({
        "ch": "depth_books",
        "symbol": "BTCUSDT",
        "ts": 1780731553373,
        "data": {"a": [["100", "1"]], "b": [["99", "1"]]},
    })
    assert snap is not None
    assert snap.taken_at.year == 2026


def test_depth_books_stream_yields_snapshots_from_envelopes():
    """The stream wrapper extracts only depth_books envelopes from the underlying WS."""
    import asyncio
    import json

    async def run() -> list:
        # Spin a mini local WS that emits a connect ack then a depth_books message.
        from websockets.asyncio.server import serve

        async def handler(ws):
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("op") == "subscribe":
                    await ws.send(json.dumps({"op": "connect", "data": {"result": True}}))
                    await ws.send(json.dumps({
                        "ch": "depth_books",
                        "symbol": "BTCUSDT",
                        "ts": 1780731553373,
                        "data": {"a": [["60959.4", "1.2215"]], "b": [["60959.3", "4.6782"]]},
                    }))

        server = await serve(handler, "127.0.0.1", 0)
        sock = next(iter(server.sockets))
        url = f"ws://127.0.0.1:{sock.getsockname()[1]}/"

        out = []
        async with BitunixDepthBooksStream("BTCUSDT", url=url, heartbeat_seconds=999) as s:
            async for snap in s.books():
                out.append(snap)
                break  # one is enough to prove the pipeline
        server.close()
        await server.wait_closed()
        return out

    snaps = asyncio.run(run())
    assert len(snaps) == 1
    assert snaps[0].pair == "BTCUSDT"
    assert snaps[0].asks[0].price == Decimal("60959.4")
    assert snaps[0].is_native is True
