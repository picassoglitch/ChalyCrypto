"""Telegram ingest service tests — drive a mocked client through the routing logic."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

import pytest

from chalybcrypto_telegram_ingest import (
    IdentitySessionVault,
    IncomingMessage,
    TelegramIngestService,
)


NOW = datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)


class _FakeClient:
    def __init__(self) -> None:
        self.started_with_session: str | None = None
        self.disconnected = False
        self.handlers: list = []

    async def start(self, *, session_string: str | None = None) -> None:
        self.started_with_session = session_string

    async def disconnect(self) -> None:
        self.disconnected = True

    def add_event_handler(self, handler) -> None:
        self.handlers.append(handler)


def _msg(text: str = "BTCUSDT LONG entry 60000 SL 59500 TP 60500") -> IncomingMessage:
    return IncomingMessage(
        channel_id="-100123",
        channel_title="Test Channel",
        text=text,
        sent_at=NOW,
    )


async def test_start_without_session_calls_client_start_with_none():
    client = _FakeClient()
    svc = TelegramIngestService(client=client)
    await svc.start()
    assert client.started_with_session is None


async def test_start_with_encrypted_session_decrypts_via_vault():
    client = _FakeClient()
    svc = TelegramIngestService(client=client, vault=IdentitySessionVault())
    await svc.start(encrypted_session=b"plain-session-string")
    assert client.started_with_session == "plain-session-string"


async def test_start_with_encrypted_session_but_no_vault_raises():
    client = _FakeClient()
    svc = TelegramIngestService(client=client)
    with pytest.raises(RuntimeError, match="SessionVault"):
        await svc.start(encrypted_session=b"x")


async def test_stop_disconnects_client():
    client = _FakeClient()
    svc = TelegramIngestService(client=client)
    await svc.stop()
    assert client.disconnected is True


async def test_handle_message_parses_and_invokes_on_signal():
    client = _FakeClient()
    received: list = []

    async def on_signal(parsed, msg):
        received.append((parsed, msg))

    svc = TelegramIngestService(client=client, on_signal=on_signal)
    parsed = await svc.handle_message(_msg())
    assert parsed is not None
    assert parsed.pair == "BTCUSDT"
    assert len(received) == 1
    assert received[0][1].channel_id == "-100123"


async def test_handle_message_unparseable_calls_callback_and_returns_none():
    client = _FakeClient()
    unparseable: list = []

    async def on_unparseable(msg):
        unparseable.append(msg)

    svc = TelegramIngestService(client=client, on_unparseable=on_unparseable)
    parsed = await svc.handle_message(_msg("just some random gif 🚀"))
    assert parsed is None
    assert len(unparseable) == 1


async def test_handle_message_dedups_by_dedup_hash():
    client = _FakeClient()
    received: list = []

    async def on_signal(parsed, msg):
        received.append(parsed)

    svc = TelegramIngestService(client=client, on_signal=on_signal)
    msg = _msg()
    await svc.handle_message(msg)
    await svc.handle_message(msg)  # same dedup_hash
    assert len(received) == 1
