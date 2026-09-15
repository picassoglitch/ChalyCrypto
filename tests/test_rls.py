"""Phase 1 acceptance: RLS keeps users from reading each other's rows.

Strategy:
  * Provision two auth.users (alice, bob), insert matching chalybcrypto.users rows
    via the superuser session (RLS is bypassed for superusers — that's correct;
    user provisioning runs as service_role in prod).
  * For each per-user table, insert a row owned by alice + a row owned by bob
    using a service_role session (RLS still applies, but our policies don't gate
    inserts against service_role; the with-check policy enforces user_id match).
    Concretely: we open a session and SET LOCAL request.jwt.claim.sub = alice,
    SET ROLE to a non-superuser so RLS actually applies, then insert.
  * Reopen as alice (non-superuser, JWT.sub = alice) and read every table — must
    see exactly her row.
  * Re-bind to bob's JWT sub on the same connection — must see exactly his row.
  * Assert cross-user reads return zero rows.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from uuid import UUID, uuid4

import psycopg
import pytest


# Each row is (table_name, extra column dict). user_id is added automatically.
PER_USER_TABLES: list[tuple[str, dict]] = [
    ("exchange_connections", {"exchange": "bitunix", "api_key_enc": b"\x00", "api_secret_enc": b"\x00"}),
    ("telegram_channels", {"tg_channel_id": "-100123"}),
    ("parsed_signals", {"source": "telegram", "pair": "BTCUSDT", "side": "long", "dedup_hash": uuid4().hex}),
    ("trades", {"mode": "paper", "pair": "BTCUSDT", "side": "long"}),
    ("positions", {"pair": "BTCUSDT", "side": "long"}),
    ("orders", {"exchange_order_id": uuid4().hex, "side": "buy"}),
    ("strategy_results", {"strategy": "ema_trend", "mode": "paper"}),
    ("backtests", {"strategy": "ema_trend", "pair": "BTCUSDT"}),
    ("risk_profiles", {"name": "default", "params": json.dumps({})}),
    ("notifications", {"channel": "telegram", "kind": "approval"}),
    ("audit_logs", {"actor": "risk_engine", "action": "approve"}),
]


@contextmanager
def _user_session(dsn: str, user_id: UUID, role: str = "authenticated"):
    """Open a session that runs as a NON-superuser with the given JWT sub claim.

    Superusers bypass RLS entirely; we must drop to a normal role so policies apply.
    """
    with psycopg.connect(dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute("set role rls_test_user")
            cur.execute(f"set local request.jwt.claim.sub = '{user_id}'")
            cur.execute(f"set local request.jwt.claim.role = '{role}'")
        yield conn


def _provision_user(admin_dsn: str, display_name: str) -> UUID:
    """Provision an auth.users + chalybcrypto.users row as superuser. Returns id."""
    uid = uuid4()
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("insert into auth.users (id, email) values (%s, %s)", (uid, f"{display_name}@test"))
            cur.execute(
                "insert into chalybcrypto.users (id, display_name) values (%s, %s)",
                (uid, display_name),
            )
    return uid


def _create_role_if_missing(admin_dsn: str) -> None:
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            "do $$ begin if not exists (select 1 from pg_roles where rolname = 'rls_test_user') "
            "then create role rls_test_user nologin; end if; end $$"
        )
        conn.execute("grant usage on schema chalybcrypto to rls_test_user")
        conn.execute(
            "grant select, insert, update, delete on all tables in schema chalybcrypto to rls_test_user"
        )
        conn.execute(
            "alter default privileges in schema chalybcrypto grant select, insert, update, delete on tables to rls_test_user"
        )


def test_rls_blocks_cross_user_reads(db_dsn):
    _create_role_if_missing(db_dsn)
    alice = _provision_user(db_dsn, "alice")
    bob = _provision_user(db_dsn, "bob")

    # Insert one row per table for each user, using a non-superuser session that
    # carries the right JWT sub — proves the with-check policy works on the way in.
    for owner in (alice, bob):
        with _user_session(db_dsn, owner) as conn:
            with conn.cursor() as cur:
                for table, extra in PER_USER_TABLES:
                    cols = ["user_id", *extra.keys()]
                    placeholders = ", ".join(["%s"] * len(cols))
                    values = [owner, *extra.values()]
                    cur.execute(
                        f"insert into chalybcrypto.{table} ({', '.join(cols)}) values ({placeholders})",
                        values,
                    )
            conn.commit()

    # Now read every table as alice; she must see exactly 1 row, and it must be hers.
    for owner_uid, label in ((alice, "alice"), (bob, "bob")):
        with _user_session(db_dsn, owner_uid) as conn:
            with conn.cursor() as cur:
                for table, _ in PER_USER_TABLES:
                    cur.execute(f"select user_id from chalybcrypto.{table}")
                    rows = cur.fetchall()
                    assert len(rows) == 1, f"{label} should see exactly 1 row in {table}, saw {len(rows)}"
                    assert rows[0][0] == owner_uid, (
                        f"{label} saw a row owned by {rows[0][0]} in {table}"
                    )

    # Explicit cross-read: as alice, scope a delete to bob's id — must affect 0 rows.
    with _user_session(db_dsn, alice) as conn:
        with conn.cursor() as cur:
            cur.execute("delete from chalybcrypto.trades where user_id = %s", (bob,))
            assert cur.rowcount == 0, "alice was able to delete bob's trades — RLS hole"
        conn.rollback()

    # And bob's rows still exist when seen as bob.
    with _user_session(db_dsn, bob) as conn:
        with conn.cursor() as cur:
            cur.execute("select count(*) from chalybcrypto.trades")
            assert cur.fetchone()[0] == 1


def test_rls_blocks_insert_with_wrong_user_id(db_dsn):
    _create_role_if_missing(db_dsn)
    alice = _provision_user(db_dsn, "alice2")
    bob = _provision_user(db_dsn, "bob2")

    with _user_session(db_dsn, alice) as conn:
        with conn.cursor() as cur:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                cur.execute(
                    "insert into chalybcrypto.trades (user_id, mode, pair, side) values (%s, 'paper', 'BTCUSDT', 'long')",
                    (bob,),
                )


def test_ai_evaluations_only_visible_through_owned_trade(db_dsn):
    _create_role_if_missing(db_dsn)
    alice = _provision_user(db_dsn, "alice3")
    bob = _provision_user(db_dsn, "bob3")

    # Insert a trade for alice; insert an ai_evaluation pointing at it.
    with _user_session(db_dsn, alice) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "insert into chalybcrypto.trades (user_id, mode, pair, side) values (%s, 'paper', 'BTCUSDT', 'long') returning id",
                (alice,),
            )
            trade_id = cur.fetchone()[0]
            cur.execute(
                "insert into chalybcrypto.ai_evaluations (trade_id, model, kind, content) values (%s, 'claude', 'thesis', 'hi')",
                (trade_id,),
            )
        conn.commit()

    # As bob, the ai_evaluation must be invisible.
    with _user_session(db_dsn, bob) as conn:
        with conn.cursor() as cur:
            cur.execute("select count(*) from chalybcrypto.ai_evaluations")
            assert cur.fetchone()[0] == 0

    # As alice, it's visible.
    with _user_session(db_dsn, alice) as conn:
        with conn.cursor() as cur:
            cur.execute("select count(*) from chalybcrypto.ai_evaluations")
            assert cur.fetchone()[0] == 1
