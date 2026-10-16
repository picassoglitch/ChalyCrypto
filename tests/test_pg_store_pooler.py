"""PgStore connects without server-side prepared statements, so it works
through Supabase's transaction pooler (port 6543)."""

from typing import Any

import psycopg

from chalybcrypto_api.pg_store import PgStore


async def test_connect_disables_prepared_statements(monkeypatch: Any) -> None:
    seen: dict[str, Any] = {}

    async def fake_connect(dsn: str, **kwargs: Any) -> object:
        seen["dsn"] = dsn
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", fake_connect)
    await PgStore("postgresql://u:p@h.pooler.supabase.com:6543/postgres")._conn()
    assert seen["prepare_threshold"] is None
    assert seen["autocommit"] is True
