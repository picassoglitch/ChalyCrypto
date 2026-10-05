"""PgStore usage outbox against real Postgres (skips when none is reachable)."""

from __future__ import annotations

import json

import httpx
import psycopg

from chalybcrypto_api.pg_store import PgStore
from chalybcrypto_hub import HubClient, drain_outbox, llm_tokens_event


def _evt(user: str):
    return llm_tokens_event(external_user_id=user, model="claude-haiku-4-5", input_tokens=10)


def _hub(code: int) -> HubClient:
    t = httpx.MockTransport(lambda req: httpx.Response(code, json={}))
    return HubClient(base_url="https://hub.test", token="t", client=httpx.AsyncClient(transport=t))


async def test_pg_outbox_enqueue_claim_send_dead(db_dsn):
    store = PgStore(db_dsn)
    with psycopg.connect(db_dsn, autocommit=True) as c:
        c.execute("truncate chalybcrypto.usage_outbox")
    e1, e2 = _evt("pg-u1"), _evt("pg-u1")
    assert await store.enqueue_usage_events([e1, e2]) == 2
    assert await store.enqueue_usage_events([e1]) == 0

    stats = await drain_outbox(store, _hub(200))
    assert stats.sent == 2
    assert {r["status"] for r in await store.list_usage_outbox()} == {"sent"}

    await store.enqueue_usage_events([_evt("pg-u2")])
    stats = await drain_outbox(store, _hub(503))
    assert stats.retried == 1
    pending = await store.list_usage_outbox(status="pending")
    assert len(pending) == 1 and pending[0]["attempts"] == 1
    # Backed off → not due.
    assert (await drain_outbox(store, _hub(200))).claimed == 0

    with psycopg.connect(db_dsn, autocommit=True) as c:
        c.execute("update chalybcrypto.usage_outbox set next_attempt_at = now()")
    stats = await drain_outbox(store, _hub(422))
    assert stats.dead == 1
    dead = await store.list_usage_outbox(status="dead")
    assert len(dead) == 1 and "422" in dead[0]["last_error"]
    assert json.loads(json.dumps(dead[0]["event"]))["kind"] == "llm.tokens"
