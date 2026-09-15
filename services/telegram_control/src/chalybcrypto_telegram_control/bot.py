"""Telegram Bot API control bot — notifications + inline-keyboard actions.

ARCHITECTURE §0.4: this is the **Bot API** side of the Telegram integration. The user-
session ingest lives in services/telegram_ingest. Bot API can post messages and receive
button clicks for channels/users the bot itself has access to — it cannot read arbitrary
channels (that's why ingest is a separate component).

Approve / Reject / Continue / Close / Move SL to BE / Activate protected stop / Pause —
one inline button per supported action, callback_data = `<action>:<approval_id>`.

This module ships:
  * Keyboard builders (pure functions returning Bot-API-shaped JSON)
  * Callback parser (callback_data string → typed (Action, uuid))
  * ControlBot class — thin httpx wrapper around send_message + answerCallbackQuery
    plus a dispatch loop that routes parsed callbacks to caller-provided async handlers.

Live wiring (bot token, webhook vs long-polling) lands when BOT_TOKEN is provided.
The dispatch + keyboard logic is fully tested without that.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Awaitable, Callable, Protocol
from uuid import UUID

import httpx


BOT_API_BASE = "https://api.telegram.org"


class Action(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    CONTINUE = "continue"
    CLOSE = "close"
    BREAKEVEN = "breakeven"
    PROTECT = "protect"
    PAUSE = "pause"


# Callback_data has a 64-byte hard limit on Telegram. action prefix + uuid hex (32) fits.


def _cb(action: Action, target_id: UUID) -> str:
    return f"{action.value}:{target_id.hex}"


def build_approval_keyboard(approval_id: UUID) -> dict:
    """Inline keyboard for a pending semi-auto approval."""
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Approve", "callback_data": _cb(Action.APPROVE, approval_id)},
                {"text": "❌ Reject", "callback_data": _cb(Action.REJECT, approval_id)},
            ],
        ],
    }


def build_position_keyboard(position_id: UUID) -> dict:
    """Inline keyboard for an open position. Continue / Close / Breakeven / Protect / Pause."""
    return {
        "inline_keyboard": [
            [
                {"text": "➡️ Continue", "callback_data": _cb(Action.CONTINUE, position_id)},
                {"text": "✖ Close", "callback_data": _cb(Action.CLOSE, position_id)},
            ],
            [
                {"text": "🛡 Breakeven", "callback_data": _cb(Action.BREAKEVEN, position_id)},
                {"text": "🔒 Protect", "callback_data": _cb(Action.PROTECT, position_id)},
            ],
            [
                {"text": "⏸ Pause new entries", "callback_data": _cb(Action.PAUSE, position_id)},
            ],
        ],
    }


@dataclass(frozen=True)
class ApprovalCallback:
    action: Action
    target_id: UUID
    raw: str


def parse_callback_data(raw: str) -> ApprovalCallback | None:
    """Parse a `action:hex_uuid` string into a typed callback. Returns None on garbage."""
    if not raw or ":" not in raw:
        return None
    action_s, _, uid_s = raw.partition(":")
    try:
        action = Action(action_s)
    except ValueError:
        return None
    try:
        uid = UUID(hex=uid_s)
    except ValueError:
        return None
    return ApprovalCallback(action=action, target_id=uid, raw=raw)


@dataclass(frozen=True)
class CallbackHandlerResult:
    """What the handler chose to do; the bot uses it to acknowledge the click and to
    optionally reply with a follow-up message."""

    ok: bool
    user_visible_text: str | None = None
    edit_message: bool = False


class ControlBot:
    """Thin httpx wrapper around the Bot API. Live send/receive isn't exercised in CI —
    handler dispatch + keyboard shape is, which is what actually carries the engine's
    correctness."""

    def __init__(
        self,
        *,
        bot_token: str | None = None,
        base_url: str = BOT_API_BASE,
        client: httpx.AsyncClient | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._token = bot_token
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self._handlers: dict[Action, Callable[[UUID], Awaitable[CallbackHandlerResult]]] = {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def register(self, action: Action, handler: Callable[[UUID], Awaitable[CallbackHandlerResult]]) -> None:
        self._handlers[action] = handler

    async def dispatch_callback(self, callback_data: str) -> CallbackHandlerResult | None:
        """Route a parsed callback to its handler. Returns None for unknown actions
        (caller should answer the callback query with a generic error)."""
        cb = parse_callback_data(callback_data)
        if cb is None:
            return None
        handler = self._handlers.get(cb.action)
        if handler is None:
            return None
        return await handler(cb.target_id)

    async def send_message(self, chat_id: int | str, text: str, *, reply_markup: dict | None = None) -> dict:
        """POST sendMessage. Requires bot_token configured."""
        if not self._token:
            raise RuntimeError("ControlBot.send_message: bot_token not configured")
        body: dict = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        url = f"{self._base}/bot{self._token}/sendMessage"
        r = await self._client.post(url, json=body)
        r.raise_for_status()
        return r.json()

    async def answer_callback_query(self, callback_query_id: str, *, text: str | None = None) -> dict:
        if not self._token:
            raise RuntimeError("ControlBot.answer_callback_query: bot_token not configured")
        body: dict = {"callback_query_id": callback_query_id}
        if text is not None:
            body["text"] = text
        url = f"{self._base}/bot{self._token}/answerCallbackQuery"
        r = await self._client.post(url, json=body)
        r.raise_for_status()
        return r.json()
