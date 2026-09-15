"""Idempotency store.

CLAUDE.md rule 8: every exchange write must go through an idempotency key (Redis lock on
dedup hash). The risk engine takes any object that satisfies IdempotencyStore so tests can
swap in an in-memory impl. The Redis-backed impl lives in services/worker (Phase 6).
"""

from __future__ import annotations

import time
from typing import Protocol


class IdempotencyStore(Protocol):
    """Anything that can atomically claim a key for a TTL window."""

    async def try_acquire(self, key: str, ttl_seconds: int) -> bool: ...


class InMemoryIdempotencyStore:
    """Process-local store for tests and single-worker dev. Not safe across processes."""

    def __init__(self) -> None:
        # key -> expiry epoch seconds
        self._keys: dict[str, float] = {}

    async def try_acquire(self, key: str, ttl_seconds: int) -> bool:
        now = time.monotonic()
        expiry = self._keys.get(key)
        if expiry is not None and expiry > now:
            return False
        self._keys[key] = now + ttl_seconds
        return True

    def clear(self) -> None:
        self._keys.clear()
