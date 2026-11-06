"""Analyst → llm.tokens events in the usage outbox, for every attempt."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import httpx

from chalybcrypto_api.store import InMemoryStore
from chalybcrypto_llm import ClaudeAnalyst, DailyDigest

DIGEST = DailyDigest(
    day=datetime(2026, 6, 6, tzinfo=timezone.utc), trade_count=3, net_pnl=Decimal("1")
)

USAGE = {
    "input_tokens": 1500,
    "output_tokens": 80,
    "cache_creation_input_tokens": 1400,
    "cache_read_input_tokens": 0,
}


def _analyst(handler, store) -> ClaudeAnalyst:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ClaudeAnalyst(api_key="k", client=client, usage_sink=store)


async def test_success_records_llm_tokens_event():
    store = InMemoryStore()

    def ok(req):
        return httpx.Response(
            200,
            json={
                "model": "claude-haiku-4-5-20251001",
                "content": [{"type": "text", "text": "hola"}],
                "usage": USAGE,
            },
        )

    out = await _analyst(ok, store).daily_digest(DIGEST, external_user_id="u1", reservation_id="r9")
    assert out is not None and out.content == "hola"

    rows = await store.list_usage_outbox()
    assert len(rows) == 1
    ev = rows[0]["event"]
    assert rows[0]["external_user_id"] == "u1"
    assert ev["kind"] == "llm.tokens"
    assert ev["provider"] == "anthropic"
    assert ev["operation"] == "analyst.daily_digest"
    assert ev["reservation_id"] == "r9"
    assert ev["metadata"]["tokens"] == {
        "input": 1500,
        "output": 80,
        "cache_read": 0,
        "cache_write": 1400,
    }
    assert ev["metadata"]["model"] == "claude-haiku-4-5"
    assert ev["metadata"]["status"] == "ok"
    assert ev["amount"] == 1500 + 80 + 1400
    assert ev["cost_usd_micros"] == 3650


async def test_http_error_attempt_is_still_metered():
    store = InMemoryStore()

    def overloaded(req):
        return httpx.Response(529, json={"type": "error", "error": {"type": "overloaded_error"}})

    out = await _analyst(overloaded, store).daily_digest(DIGEST, external_user_id="u1")
    assert out is None
    rows = await store.list_usage_outbox()
    assert len(rows) == 1
    assert rows[0]["event"]["metadata"]["status"] == "http_529"
    assert rows[0]["event"]["amount"] == 0


async def test_transport_error_attempt_is_still_metered():
    store = InMemoryStore()

    def boom(req):
        raise httpx.ReadTimeout("slow")

    out = await _analyst(boom, store).daily_digest(DIGEST, external_user_id="u1")
    assert out is None
    rows = await store.list_usage_outbox()
    assert len(rows) == 1
    assert rows[0]["event"]["metadata"]["status"].startswith("transport_error")


async def test_each_call_gets_its_own_source_id():
    store = InMemoryStore()

    def ok(req):
        return httpx.Response(200, json={"content": [], "usage": USAGE})

    a = _analyst(ok, store)
    await a.daily_digest(DIGEST, external_user_id="u1")
    await a.daily_digest(DIGEST, external_user_id="u1")
    rows = await store.list_usage_outbox()
    assert len({r["source_id"] for r in rows}) == 2


async def test_sink_failure_never_breaks_the_analyst():
    class BrokenSink:
        async def enqueue_usage_events(self, events):
            raise RuntimeError("db down")

    def ok(req):
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": "x"}], "usage": USAGE}
        )

    out = await _analyst(ok, BrokenSink()).daily_digest(DIGEST, external_user_id="u1")
    assert out is not None and out.content == "x"


async def test_no_sink_or_no_user_records_nothing():
    store = InMemoryStore()

    def ok(req):
        return httpx.Response(200, json={"content": [], "usage": USAGE})

    await _analyst(ok, store).daily_digest(DIGEST)  # no external_user_id
    assert await store.list_usage_outbox() == []
