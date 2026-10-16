"""FastAPI dependency injection.

Auth: see auth.py — CHALYBCRYPTO_AUTH switches between JWT (production) and the
X-User-Id stub (local dev / tests / the demo dashboard).

Store selection: CHALYBCRYPTO_STORE=memory|pg. The DSN is CHALYBCRYPTO_DATABASE_URL,
or DATABASE_URL (the name the Chalyb Terraform module injects). On Cloud Run
(K_SERVICE set) the default is pg and memory is refused: an in-memory store there
loses tenants and the usage outbox on every cold start.
"""

from __future__ import annotations

import os

from .auth import current_user_id as get_current_user_id  # re-export for routes  # noqa: F401
from .store import ApiStore, InMemoryStore


def _build_store() -> ApiStore:
    on_cloud_run = bool(os.environ.get("K_SERVICE"))
    kind = os.environ.get("CHALYBCRYPTO_STORE", "pg" if on_cloud_run else "memory").strip().lower()
    if kind != "pg" and on_cloud_run:
        raise RuntimeError("CHALYBCRYPTO_STORE must be pg on Cloud Run")
    if kind == "pg":
        dsn = os.environ.get("CHALYBCRYPTO_DATABASE_URL") or os.environ.get("DATABASE_URL")
        if not dsn:
            raise RuntimeError(
                "CHALYBCRYPTO_STORE=pg but neither CHALYBCRYPTO_DATABASE_URL nor DATABASE_URL is set"
            )
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
