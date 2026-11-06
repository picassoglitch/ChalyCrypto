from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest


# psycopg async requires SelectorEventLoop on Windows (the default is Proactor on 3.8+).
# Set the policy before pytest-asyncio creates its loop.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


REPO_ROOT = Path(__file__).resolve().parents[1]
SUPABASE_DIR = REPO_ROOT / "supabase"


def _admin_dsn() -> str | None:
    """Superuser DSN used to provision a throwaway test database."""
    return os.environ.get("CHALYBCRYPTO_TEST_PG_ADMIN_DSN") or os.environ.get(
        "TEST_PG_ADMIN_DSN"
    ) or "postgresql://postgres@127.0.0.1:5432/postgres"


def _pg_reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=2):
            return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def db_dsn() -> str:
    """Create a fresh per-session test database, apply migrations + shim, return its DSN.

    Skips the test session's DB-dependent tests if no Postgres is reachable.
    """
    admin = _admin_dsn()
    if not _pg_reachable(admin):
        pytest.skip(
            f"No Postgres reachable at {admin}. Start one or set CHALYBCRYPTO_TEST_PG_ADMIN_DSN."
        )

    db_name = f"chalybcrypto_test_{uuid4().hex[:12]}"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'create database "{db_name}"')

    test_dsn = admin.rsplit("/", 1)[0] + f"/{db_name}"

    shim = (SUPABASE_DIR / "test_auth_shim.sql").read_text(encoding="utf-8")
    mig_files = sorted((SUPABASE_DIR / "migrations").glob("*.sql"))
    assert mig_files, "no migration files found"

    with psycopg.connect(test_dsn, autocommit=True) as conn:
        conn.execute(shim)
        for f in mig_files:
            conn.execute(f.read_text(encoding="utf-8"))

    yield test_dsn

    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(
            f'select pg_terminate_backend(pid) from pg_stat_activity where datname = %s and pid <> pg_backend_pid()',
            (db_name,),
        )
        conn.execute(f'drop database if exists "{db_name}"')


# ── backtests: no network in the suite ─────────────────────────────────────
def synthetic_klines(n: int = 600, *, start_price: float = 60000.0, step_minutes: int = 5):
    """Deterministic trending + oscillating candles (oldest first) so the real
    Backtester and strategies produce trades without touching an exchange."""
    import math
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal

    from chalybcrypto_shared import Kline

    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    out = []
    price = start_price
    for i in range(n):
        drift = 0.0006 * math.sin(i / 40) + 0.0002 * math.sin(i / 7)
        o = price
        c = o * (1 + drift)
        h = max(o, c) * 1.0008
        lo = min(o, c) * 0.9992
        ts = t0 + timedelta(minutes=step_minutes * i)
        out.append(
            Kline(
                open_time=ts,
                open=Decimal(f"{o:.2f}"),
                high=Decimal(f"{h:.2f}"),
                low=Decimal(f"{lo:.2f}"),
                close=Decimal(f"{c:.2f}"),
                volume=Decimal(str(10 + (i % 13))),
                close_time=ts + timedelta(minutes=step_minutes),
            )
        )
        price = c
    return out


@pytest.fixture(autouse=True)
def _offline_backtest_history(monkeypatch):
    import chalybcrypto_api.backtests as bt

    async def fake(pair, interval, bars):
        return synthetic_klines(min(bars, 600)), "test"

    monkeypatch.setattr(bt, "fetch_history", fake)
