from __future__ import annotations

import asyncio

from chalybcrypto_engine.risk import InMemoryIdempotencyStore


async def test_acquire_succeeds_first_then_fails_until_ttl_expires():
    store = InMemoryIdempotencyStore()
    assert await store.try_acquire("abc", ttl_seconds=60) is True
    assert await store.try_acquire("abc", ttl_seconds=60) is False  # still held


async def test_distinct_keys_dont_collide():
    store = InMemoryIdempotencyStore()
    assert await store.try_acquire("a", ttl_seconds=60) is True
    assert await store.try_acquire("b", ttl_seconds=60) is True


async def test_acquire_after_short_ttl_succeeds_again():
    store = InMemoryIdempotencyStore()
    assert await store.try_acquire("k", ttl_seconds=0) is True
    # ttl=0 immediately expired; next claim should succeed.
    await asyncio.sleep(0.001)
    assert await store.try_acquire("k", ttl_seconds=10) is True
