"""FastAPI dependency injection.

Auth: see auth.py — CHALYBCRYPTO_AUTH switches between JWT (production) and the
X-User-Id stub (local dev / tests / the demo dashboard).

Store selection: CHALYBCRYPTO_STORE=memory|pg.
"""

from __future__ import annotations

import os

from .auth import current_user_id as get_current_user_id  # re-export for routes  # noqa: F401
from .store import ApiStore, InMemoryStore


def _build_store() -> ApiStore:
    kind = os.environ.get("CHALYBCRYPTO_STORE", "memory").strip().lower()
    if kind == "pg":
        dsn = os.environ.get("CHALYBCRYPTO_DATABASE_URL")
        if not dsn:
            raise RuntimeError("CHALYBCRYPTO_STORE=pg but CHALYBCRYPTO_DATABASE_URL is unset")
        from .pg_store import PgStore  # noqa: WPS433
        return PgStore(dsn)
    return InMemoryStore()


_STORE: ApiStore = _build_store()


def get_store() -> ApiStore:
    return _STORE


def set_store_for_tests(store: ApiStore) -> None:
    """Test helper — point the API at a fresh store per test."""
    global _STORE
    _STORE = store
