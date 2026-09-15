"""Telegram ingest service skeleton.

Wraps a Telethon TelegramClient and routes inbound channel messages through the
deterministic signal parser (CLAUDE.md rule 2: no LLM in the path).

Session security (CLAUDE.md rule 7): session strings are encrypted at rest. The store
accepts encrypted bytes; envelope encryption is a stub for now (the master key flows
via CHALYBCRYPTO_MASTER_ENCRYPTION_KEY) and the integration point is `SessionVault`.
The real Telethon login flow lives in the setup wizard (Phase 7), which writes the
encrypted session string into the DB. This service reads it back, decrypts, and runs.

Live login + send/receive are NOT tested in CI — `_make_client_factory` injects a
mock for tests and the wizard injects Telethon's real client in production.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Protocol

from .parser import ParsedTelegramSignal, parse_signal


class SessionVault(Protocol):
    """Decrypts an encrypted session string. Real impl wraps the master key from env."""

    def decrypt(self, encrypted: bytes) -> str: ...
    def encrypt(self, plaintext: str) -> bytes: ...


class TelegramClientProtocol(Protocol):
    """The subset of Telethon's TelegramClient surface we depend on. Lets tests inject
    a mock without importing Telethon."""

    async def start(self, *, session_string: str | None = None) -> None: ...
    async def disconnect(self) -> None: ...

    def add_event_handler(self, handler: Callable[..., Awaitable[None]]) -> None: ...


@dataclass(frozen=True)
class IncomingMessage:
    """Channel + text we received. Mirrors the slice of Telethon's event we actually use."""

    channel_id: str
    channel_title: str | None
    text: str
    sent_at: datetime


class TelegramIngestService:
    """Run forever, ingest channel messages, route parsed signals to the engine pipeline.

    A consumer (typically the worker) registers `on_signal` and the engine wires it to
    add_parsed_signal → strategy validation → risk engine → approval queue, in line with
    ARCHITECTURE §3 ("Telegram-copied signals are *candidates*, not auto-trades").
    """

    def __init__(
        self,
        *,
        client: TelegramClientProtocol,
        vault: SessionVault | None = None,
        on_signal: Callable[[ParsedTelegramSignal, IncomingMessage], Awaitable[None]] | None = None,
        on_unparseable: Callable[[IncomingMessage], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self._vault = vault
        self._on_signal = on_signal
        self._on_unparseable = on_unparseable
        self._seen_dedup_hashes: set[str] = set()  # in-process dedup; Redis lock in prod

    async def start(self, *, encrypted_session: bytes | None = None) -> None:
        session_string: str | None = None
        if encrypted_session is not None:
            if self._vault is None:
                raise RuntimeError("encrypted_session provided but no SessionVault configured")
            session_string = self._vault.decrypt(encrypted_session)
        await self._client.start(session_string=session_string)

    async def stop(self) -> None:
        await self._client.disconnect()

    async def handle_message(self, msg: IncomingMessage) -> ParsedTelegramSignal | None:
        """Public so tests can drive it directly without spinning a Telethon event loop."""
        parsed = parse_signal(msg.text, now=msg.sent_at)
        if parsed is None:
            if self._on_unparseable is not None:
                await self._on_unparseable(msg)
            return None
        # In-process dedup. Real impl gates on the Redis idempotency store.
        if parsed.dedup_hash in self._seen_dedup_hashes:
            return None
        self._seen_dedup_hashes.add(parsed.dedup_hash)
        if self._on_signal is not None:
            await self._on_signal(parsed, msg)
        return parsed


# ── tiny in-memory vault stub for tests / dev only ──────────────────────────


class IdentitySessionVault:
    """No-op vault — returns the bytes as a UTF-8 string. Real prod uses Fernet/AES-GCM
    keyed by the master key. CLAUDE.md rule 7: secrets never logged, never returned."""

    def encrypt(self, plaintext: str) -> bytes:
        return plaintext.encode("utf-8")

    def decrypt(self, encrypted: bytes) -> str:
        return encrypted.decode("utf-8")
