"""LLM analyst tests — mocked httpx, fail-to-None contract, prompt-cache headers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest

from chalybcrypto_llm import (
    ClaudeAnalyst,
    ContinueBriefing,
    DailyDigest,
)
from chalybcrypto_shared import (
    MarginType,
    MarketSnapshot,
    Mode,
    Side,
    Signal,
    TradeDecision,
    dedup_hash,
)


NOW = datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)


def _signal() -> Signal:
    return Signal(
        pair="BTCUSDT", side=Side.LONG, strategy="ema_adx_trend",
        entry=Decimal("60000"), stop_loss=Decimal("59700"),
        take_profits=[Decimal("60900")], leverage=Decimal("10"),
        margin_type=MarginType.ISOLATED, timeframe="5m",
        thesis_tags=["ema_aligned", "adx_25", "cross_up"],
        source="scanner", dedup_hash=dedup_hash("BTCUSDT", "long"), created_at=NOW,
    )


def _snapshot() -> MarketSnapshot:
    return MarketSnapshot(
        pair="BTCUSDT", exchange="bitunix", taken_at=NOW,
        klines=[], mark_price=Decimal("60050"), funding_rate=Decimal("0.0001"),
    )


def _decision() -> TradeDecision:
    return TradeDecision(
        signal_id=uuid4(), mode=Mode.PAPER, approved=True, reason="ok",
        intended_take_profits=[], idempotency_key=dedup_hash("x"), decided_at=NOW,
        ev_net_bps=Decimal("12"), liquidation_distance_bps=Decimal("950"),
    )


def _mock_messages_response(text: str = "Tesis: el rebote confirmó EMA35.") -> dict:
    return {
        "id": "msg_test",
        "model": "claude-haiku-4-5",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "usage": {
            "input_tokens": 1500,
            "output_tokens": 80,
            "cache_creation_input_tokens": 1400,
            "cache_read_input_tokens": 0,
        },
    }


def _make(handler) -> ClaudeAnalyst:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return ClaudeAnalyst(api_key="test-key", client=client)


# ── basic round-trip ───────────────────────────────────────────────────────


async def test_write_thesis_returns_text_and_token_counts():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_mock_messages_response())

    analyst = _make(handler)
    try:
        out = await analyst.write_thesis(_signal(), _snapshot(), _decision())
    finally:
        await analyst.aclose()

    assert out is not None
    assert out.kind == "thesis"
    assert out.content == "Tesis: el rebote confirmó EMA35."
    assert out.input_tokens == 1500
    assert out.cache_creation_tokens == 1400
    assert "/v1/messages" in captured["url"]
    assert captured["headers"]["x-api-key"] == "test-key"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"

    # system prompt is sent with cache_control = ephemeral (prompt-caching)
    sys = captured["body"]["system"]
    assert isinstance(sys, list)
    assert sys[0]["cache_control"] == {"type": "ephemeral"}
    # user payload is JSON containing the actual signal
    assert "BTCUSDT" in captured["body"]["messages"][0]["content"]


async def test_continue_briefing_uses_distinct_system_prompt():
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_mock_messages_response("Manten."))

    analyst = _make(handler)
    try:
        out = await analyst.continue_briefing(
            ContinueBriefing(pair="BTCUSDT", side=Side.LONG, current_unrealized_net=Decimal("75")),
            _snapshot(),
        )
    finally:
        await analyst.aclose()

    assert out is not None
    assert out.kind == "continue_brief"
    # Thesis vs continue-brief have distinct system prompts (cache key differs).
    assert "continue or exit" in captured["body"]["system"][0]["text"]


async def test_daily_digest_round_trip():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_mock_messages_response("Resumen del día..."))

    analyst = _make(handler)
    try:
        out = await analyst.daily_digest(
            DailyDigest(day=NOW, trade_count=8, net_pnl=Decimal("142.50"))
        )
    finally:
        await analyst.aclose()

    assert out is not None
    assert out.kind == "daily_digest"
    assert out.content.startswith("Resumen")


# ── fail-to-None contract (CLAUDE.md rule 2) ──────────────────────────────


async def test_returns_none_when_api_key_missing():
    analyst = ClaudeAnalyst(api_key=None)
    try:
        out = await analyst.write_thesis(_signal(), _snapshot(), _decision())
    finally:
        await analyst.aclose()
    assert out is None


async def test_returns_none_on_http_error():
    analyst = _make(lambda r: httpx.Response(503, text="overloaded"))
    try:
        out = await analyst.write_thesis(_signal(), _snapshot(), _decision())
    finally:
        await analyst.aclose()
    assert out is None  # NEVER raises into the hot path


async def test_returns_none_on_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns")

    analyst = _make(handler)
    try:
        out = await analyst.write_thesis(_signal(), _snapshot(), _decision())
    finally:
        await analyst.aclose()
    assert out is None


# ── architectural guarantee: analyst not imported by hot-path packages ───


def test_engine_does_not_import_chalybcrypto_llm():
    """CLAUDE.md rule 2: no LLM in the execution path. The engine package must not
    import the analyst — that's the only structural guarantee no one accidentally
    awaits Claude before authorizing a fill.

    Run in a clean subprocess so other tests' imports don't pollute sys.modules.
    """
    import subprocess
    import sys

    code = (
        "import sys\n"
        "import chalybcrypto_engine\n"
        "import chalybcrypto_engine.risk\n"
        "import chalybcrypto_engine.strategy\n"
        "import chalybcrypto_engine.backtest\n"
        "import chalybcrypto_engine.paper\n"
        "assert 'chalybcrypto_llm' not in sys.modules, "
        "'chalybcrypto_engine pulls in chalybcrypto_llm transitively!'\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, f"engine imports LLM:\n{r.stderr}"
