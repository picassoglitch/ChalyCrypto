"""Telegram Bot API control bot tests — keyboards, callback parsing, dispatch."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from chalybcrypto_telegram_control import (
    Action,
    CallbackHandlerResult,
    ControlBot,
    build_approval_keyboard,
    build_position_keyboard,
    parse_callback_data,
)


def test_approval_keyboard_has_approve_and_reject_buttons():
    aid = uuid4()
    kb = build_approval_keyboard(aid)
    rows = kb["inline_keyboard"]
    flat = [b for row in rows for b in row]
    actions = [b["callback_data"].split(":")[0] for b in flat]
    assert "approve" in actions
    assert "reject" in actions
    # Every callback_data carries the same target_id.
    for b in flat:
        assert b["callback_data"].endswith(aid.hex)


def test_position_keyboard_has_all_six_supported_actions():
    pid = uuid4()
    kb = build_position_keyboard(pid)
    flat = [b for row in kb["inline_keyboard"] for b in row]
    actions = {b["callback_data"].split(":")[0] for b in flat}
    assert actions == {"continue", "close", "breakeven", "protect", "pause"}


def test_callback_data_round_trip():
    aid = uuid4()
    cb = parse_callback_data(f"approve:{aid.hex}")
    assert cb is not None
    assert cb.action == Action.APPROVE
    assert cb.target_id == aid


def test_callback_data_garbage_returns_none():
    assert parse_callback_data("") is None
    assert parse_callback_data("noop") is None
    assert parse_callback_data("approve:not-a-uuid") is None
    assert parse_callback_data("unknown_action:" + uuid4().hex) is None


async def test_callback_data_under_telegram_64_byte_limit():
    """Telegram hard-limits callback_data to 64 bytes."""
    aid = uuid4()
    for kb_builder in (build_approval_keyboard, build_position_keyboard):
        kb = kb_builder(aid)
        for row in kb["inline_keyboard"]:
            for b in row:
                assert len(b["callback_data"].encode("utf-8")) <= 64


async def test_dispatch_routes_to_registered_handler():
    bot = ControlBot()
    received: list = []

    async def on_approve(target_id: UUID) -> CallbackHandlerResult:
        received.append(target_id)
        return CallbackHandlerResult(ok=True, user_visible_text="approved")

    bot.register(Action.APPROVE, on_approve)
    aid = uuid4()
    result = await bot.dispatch_callback(f"approve:{aid.hex}")
    assert result is not None
    assert result.ok is True
    assert result.user_visible_text == "approved"
    assert received == [aid]


async def test_dispatch_unknown_action_returns_none():
    bot = ControlBot()
    result = await bot.dispatch_callback("approve:" + uuid4().hex)  # no handler registered
    assert result is None


async def test_dispatch_garbage_callback_returns_none():
    bot = ControlBot()
    assert await bot.dispatch_callback("garbage") is None


async def test_send_message_without_token_raises():
    bot = ControlBot()
    with pytest.raises(RuntimeError, match="bot_token not configured"):
        await bot.send_message(123, "hi")


async def test_send_message_posts_inline_keyboard_when_provided():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = __import__("json").loads(request.content)
        return httpx.Response(200, json={"ok": True, "result": {}})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    bot = ControlBot(bot_token="TEST_TOKEN", client=client)
    try:
        await bot.send_message(123, "Approve?", reply_markup=build_approval_keyboard(uuid4()))
    finally:
        await bot.aclose()

    assert "/botTEST_TOKEN/sendMessage" in captured["url"]
    assert captured["body"]["chat_id"] == 123
    assert "reply_markup" in captured["body"]
    assert captured["body"]["reply_markup"]["inline_keyboard"]


async def test_answer_callback_query_posts_to_bot_api():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True, "result": True})

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    bot = ControlBot(bot_token="T", client=client)
    try:
        await bot.answer_callback_query("CQ_ID", text="ack")
    finally:
        await bot.aclose()

    assert "/botT/answerCallbackQuery" in captured["url"]
