"""Usage outbox drain: batching ≤100, per-user grouping, backoff, dead-letter."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from chalybcrypto_api.store import InMemoryStore
from chalybcrypto_hub import (
    HubClient,
    UsageEvent,
    backoff_seconds,
    drain_outbox,
    llm_tokens_event,
)


def _hub(handler) -> tuple[HubClient, list[dict]]:
    seen: list[dict] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(
            {"path": request.url.path, "auth": request.headers.get("authorization"), **body}
        )
        return handler(request, body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return HubClient(base_url="https://hub.test", token="tok", client=client), seen


def _evt(user: str = "u1", **kw) -> UsageEvent:
    return llm_tokens_event(external_user_id=user, model="claude-haiku-4-5", input_tokens=10, **kw)


def _make_due(store: InMemoryStore) -> None:
    for r in store._usage_outbox:
        r["next_attempt_at"] = datetime.now(UTC) - timedelta(seconds=1)


async def test_drain_sends_and_marks_sent():
    store = InMemoryStore()
    await store.enqueue_usage_events([_evt(), _evt()])
    hub, seen = _hub(lambda req, body: httpx.Response(200, json={"ok": True, "inserted": 2}))

    stats = await drain_outbox(store, hub)

    assert (stats.claimed, stats.sent, stats.dead, stats.retried) == (2, 2, 0, 0)
    assert len(seen) == 1
    assert seen[0]["path"] == "/api/engines/chalybcrypto/usage"
    assert seen[0]["auth"] == "Bearer tok"
    assert seen[0]["external_user_id"] == "u1"
    assert len(seen[0]["events"]) == 2
    assert {r["status"] for r in await store.list_usage_outbox()} == {"sent"}
    # Nothing left to claim.
    assert (await drain_outbox(store, hub)).claimed == 0


async def test_enqueue_is_idempotent_on_source_id():
    store = InMemoryStore()
    e = _evt()
    assert await store.enqueue_usage_events([e]) == 1
    assert await store.enqueue_usage_events([e]) == 0


async def test_batches_never_exceed_100_and_group_per_user():
    store = InMemoryStore()
    await store.enqueue_usage_events([_evt("u1") for _ in range(150)])
    await store.enqueue_usage_events([_evt("u2") for _ in range(30)])
    hub, seen = _hub(lambda req, body: httpx.Response(200, json={"ok": True}))

    first = await drain_outbox(store, hub)
    assert first.claimed == 100
    assert all(len(call["events"]) <= 100 for call in seen)
    while (await drain_outbox(store, hub)).claimed:
        pass
    assert all(len(call["events"]) <= 100 for call in seen)
    per_user = {"u1": 0, "u2": 0}
    for call in seen:
        per_user[call["external_user_id"]] += len(call["events"])
    assert per_user == {"u1": 150, "u2": 30}
    assert len(await store.list_usage_outbox(status="sent")) == 180


@pytest.mark.parametrize("code", [408, 429, 500, 503])
async def test_retryable_status_schedules_backoff(code):
    store = InMemoryStore()
    await store.enqueue_usage_events([_evt()])
    hub, _ = _hub(lambda req, body: httpx.Response(code, json={"error": "later"}))

    stats = await drain_outbox(store, hub)

    assert stats.retried == 1 and stats.dead == 0
    row = (await store.list_usage_outbox())[0]
    assert row["status"] == "pending"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] > datetime.now(UTC)
    # Not due yet → not claimed again immediately.
    assert (await drain_outbox(store, hub)).claimed == 0


async def test_transport_error_is_retried():
    store = InMemoryStore()
    await store.enqueue_usage_events([_evt()])

    def boom(req, body):
        raise httpx.ConnectError("down")

    hub, _ = _hub(boom)
    stats = await drain_outbox(store, hub)
    assert stats.retried == 1
    assert (await store.list_usage_outbox())[0]["status"] == "pending"


async def test_recovers_after_retry():
    store = InMemoryStore()
    await store.enqueue_usage_events([_evt()])
    codes = iter([503, 200])
    hub, _ = _hub(lambda req, body: httpx.Response(next(codes), json={}))
    await drain_outbox(store, hub)
    _make_due(store)
    stats = await drain_outbox(store, hub)
    assert stats.sent == 1
    row = (await store.list_usage_outbox())[0]
    assert row["status"] == "sent" and row["attempts"] == 1


async def test_permanent_4xx_dead_letters_and_logs(caplog):
    store = InMemoryStore()
    await store.enqueue_usage_events([_evt()])
    hub, _ = _hub(lambda req, body: httpx.Response(422, json={"error": "occurred_at too old"}))

    with caplog.at_level(logging.ERROR, logger="chalybcrypto_hub.outbox"):
        stats = await drain_outbox(store, hub)

    assert stats.dead == 1
    row = (await store.list_usage_outbox())[0]
    assert row["status"] == "dead"
    assert "422" in row["last_error"]
    assert any("permanently rejected" in r.message for r in caplog.records)
    # Dead rows are kept but never re-sent.
    assert (await drain_outbox(store, hub)).claimed == 0
    assert len(await store.list_usage_outbox()) == 1


async def test_poison_event_is_isolated_from_its_batch():
    store = InMemoryStore()
    good = [_evt(), _evt()]
    bad = _evt(operation="poison")
    await store.enqueue_usage_events([good[0], bad, good[1]])

    def handler(req, body):
        if any(e.get("operation") == "poison" for e in body["events"]):
            return httpx.Response(400, json={"error": "invalid event shape"})
        return httpx.Response(200, json={"ok": True})

    hub, _ = _hub(handler)
    stats = await drain_outbox(store, hub)

    assert (stats.sent, stats.dead) == (2, 1)
    by_source = {r["source_id"]: r["status"] for r in await store.list_usage_outbox()}
    assert by_source[bad.source_id] == "dead"
    assert by_source[good[0].source_id] == by_source[good[1].source_id] == "sent"


async def test_unknown_user_404_dead_letters_whole_group_without_split():
    store = InMemoryStore()
    await store.enqueue_usage_events([_evt("ghost"), _evt("ghost")])
    hub, seen = _hub(lambda req, body: httpx.Response(404, json={"error": "unknown user_id"}))
    stats = await drain_outbox(store, hub)
    assert stats.dead == 2
    assert len(seen) == 1


def test_backoff_grows_and_caps():
    assert backoff_seconds(1, jitter=False) == 5
    assert backoff_seconds(2, jitter=False) == 10
    assert backoff_seconds(5, jitter=False) == 80
    assert backoff_seconds(50, jitter=False) == 3600
