"""Bitunix futures public WebSocket transport.

Confirmed from docs (https://www.bitunix.com/api-docs/futures/websocket/prepare/WebSocket.html
and the Tickers channel doc), with depth_books verified from a live socket capture
2026-06-06:

  * URL: wss://fapi.bitunix.com/public/
  * Subscribe envelope:  {"op":"subscribe",   "args":[{"symbol":"BTCUSDT","ch":"<name>"}]}
  * Unsubscribe envelope:{"op":"unsubscribe", "args":[{"symbol":"BTCUSDT","ch":"<name>"}]}
  * Standard inbound:    {"ch":"<name>","ts":<ms>,"symbol":"...","data":<channel-shape>}
  * Connection ack:      {"op":"connect","data":{"result":true}}  (ignore for app logic)
  * Heartbeat: client sends {"op":"ping","ts":<ms>}, server replies {"op":"pong","ts":<ms>}.
  * Max 300 channel subscriptions per connection.

depth_books payload:
  data.a = asks  (price ascending, top-of-book first)
  data.b = bids  (price descending, top-of-book first)
  Each level is [price_string, size_string]. Each message is a FULL book snapshot
  (no diff/seq) — replace the local book entirely on every tick.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import websockets
from websockets.asyncio.client import ClientConnection, connect

from chalybcrypto_shared import OrderBookLevel, OrderBookSnapshot


BITUNIX_PUBLIC_WS_URL = "wss://fapi.bitunix.com/public/"

# Bitunix docs state 24h connection validity + 300 channels/connection.
_MAX_CHANNELS_PER_CONNECTION = 300


@dataclass(frozen=True)
class Channel:
    """A subscription target. `symbol` may be None for global channels if any."""

    ch: str
    symbol: str | None = None

    def to_arg(self) -> dict[str, str]:
        out: dict[str, str] = {"ch": self.ch}
        if self.symbol is not None:
            out["symbol"] = self.symbol
        return out


def _now_ms() -> int:
    return int(time.time() * 1000)


def encode_subscribe(channels: list[Channel]) -> str:
    return json.dumps(
        {"op": "subscribe", "args": [c.to_arg() for c in channels]},
        separators=(",", ":"),
    )


def encode_unsubscribe(channels: list[Channel]) -> str:
    return json.dumps(
        {"op": "unsubscribe", "args": [c.to_arg() for c in channels]},
        separators=(",", ":"),
    )


def encode_ping(ts_ms: int | None = None) -> str:
    return json.dumps({"op": "ping", "ts": ts_ms if ts_ms is not None else _now_ms()}, separators=(",", ":"))


def is_pong(msg: dict) -> bool:
    return msg.get("op") == "pong"


class BitunixPublicWS:
    """Async iterator transport. Use as a context manager:

        async with BitunixPublicWS() as ws:
            await ws.subscribe(Channel("tickers", "BTCUSDT"))
            async for envelope in ws.messages():
                ...
    """

    def __init__(
        self,
        url: str = BITUNIX_PUBLIC_WS_URL,
        *,
        heartbeat_seconds: float = 20.0,
        connect_factory=connect,
    ) -> None:
        self._url = url
        self._heartbeat_seconds = heartbeat_seconds
        self._connect_factory = connect_factory
        self._ws: ClientConnection | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._subscribed: list[Channel] = []

    # ─── lifecycle ────────────────────────────────────────────────────────

    async def __aenter__(self) -> "BitunixPublicWS":
        await self.connect()
        return self

    async def __aexit__(self, *a) -> None:
        await self.aclose()

    async def connect(self) -> None:
        if self._ws is not None:
            return
        self._ws = await self._connect_factory(self._url)
        loop = asyncio.get_running_loop()
        self._heartbeat_task = loop.create_task(self._heartbeat_loop())

    async def aclose(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            with contextlib.suppress(BaseException):
                await self._heartbeat_task
            self._heartbeat_task = None
        if self._ws is not None:
            with contextlib.suppress(BaseException):
                await self._ws.close()
            self._ws = None

    # ─── subscription ─────────────────────────────────────────────────────

    async def subscribe(self, *channels: Channel) -> None:
        if not channels:
            return
        if len(self._subscribed) + len(channels) > _MAX_CHANNELS_PER_CONNECTION:
            raise RuntimeError(
                f"would exceed Bitunix per-connection subscription cap ({_MAX_CHANNELS_PER_CONNECTION})"
            )
        await self._send(encode_subscribe(list(channels)))
        self._subscribed.extend(channels)

    async def unsubscribe(self, *channels: Channel) -> None:
        if not channels:
            return
        await self._send(encode_unsubscribe(list(channels)))
        keep = [c for c in self._subscribed if c not in channels]
        self._subscribed = keep

    # ─── messages ─────────────────────────────────────────────────────────

    async def messages(self) -> AsyncIterator[dict]:
        """Yield decoded inbound envelopes. Pong replies are consumed internally."""
        ws = self._require_ws()
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if is_pong(msg):
                continue
            yield msg

    # ─── internals ────────────────────────────────────────────────────────

    def _require_ws(self) -> ClientConnection:
        if self._ws is None:
            raise RuntimeError("websocket not connected — call connect() or use async with")
        return self._ws

    async def _send(self, payload: str) -> None:
        await self._require_ws().send(payload)

    async def _heartbeat_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._heartbeat_seconds)
                if self._ws is None:
                    return
                try:
                    await self._send(encode_ping())
                except websockets.ConnectionClosed:
                    return
        except asyncio.CancelledError:
            raise


# ─── depth_books decoder ──────────────────────────────────────────────────────


DEPTH_BOOKS_CHANNEL = "depth_books"


def decode_depth_books(envelope: dict) -> OrderBookSnapshot | None:
    """Decode a single depth_books envelope into a shared OrderBookSnapshot.

    Returns None for non-depth messages (connection acks, other channels, malformed
    payloads). The snapshot is full (no merging needed); each Bitunix tick replaces the
    local book.
    """
    if envelope.get("ch") != DEPTH_BOOKS_CHANNEL:
        return None
    data = envelope.get("data")
    if not isinstance(data, dict):
        return None
    asks_raw = data.get("a", [])
    bids_raw = data.get("b", [])
    if not isinstance(asks_raw, list) or not isinstance(bids_raw, list):
        return None

    asks = [
        OrderBookLevel(price=Decimal(str(p)), size=Decimal(str(s)))
        for p, s in asks_raw
    ]
    bids = [
        OrderBookLevel(price=Decimal(str(p)), size=Decimal(str(s)))
        for p, s in bids_raw
    ]

    ts_ms = envelope.get("ts")
    taken_at = (
        datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc)
        if isinstance(ts_ms, (int, float, str)) and str(ts_ms).isdigit()
        else datetime.now(timezone.utc)
    )

    return OrderBookSnapshot(
        exchange="bitunix",
        pair=str(envelope.get("symbol", "")),
        taken_at=taken_at,
        bids=bids,
        asks=asks,
        is_native=True,  # CLAUDE.md §0.3: this IS the fill-gating book on Bitunix
    )


class BitunixDepthBooksStream:
    """High-level: subscribe to depth_books for one symbol, yield OrderBookSnapshots.

    Each yielded snapshot is a complete book (Bitunix's depth_books is a snapshot stream,
    not a diff stream). Caller may downsample or rate-limit as needed for the strategy
    engine.
    """

    def __init__(
        self,
        symbol: str,
        *,
        url: str = BITUNIX_PUBLIC_WS_URL,
        heartbeat_seconds: float = 20.0,
    ) -> None:
        self._symbol = symbol
        self._ws = BitunixPublicWS(url=url, heartbeat_seconds=heartbeat_seconds)

    async def __aenter__(self) -> "BitunixDepthBooksStream":
        await self._ws.connect()
        await self._ws.subscribe(Channel(ch=DEPTH_BOOKS_CHANNEL, symbol=self._symbol))
        return self

    async def __aexit__(self, *a) -> None:
        await self._ws.aclose()

    async def books(self) -> AsyncIterator[OrderBookSnapshot]:
        async for envelope in self._ws.messages():
            snap = decode_depth_books(envelope)
            if snap is not None:
                yield snap
