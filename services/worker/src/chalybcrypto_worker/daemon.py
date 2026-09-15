"""Continuous scanner daemon — long-running process that ticks scan_once on a schedule.

  python -m chalybcrypto_worker.daemon BTCUSDT ETHUSDT --interval 30 \
    --persist --dsn postgresql://postgres@127.0.0.1:5432/chalybcrypto

Each pair is scanned in parallel per tick; misses on one pair don't block the others.
SIGINT exits cleanly. No Celery, no Redis, no scheduler service — just an async loop
that any process supervisor (systemd, supervisord, docker) can babysit.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from uuid import UUID

from chalybcrypto_connectors.bitunix import BitunixConnector

from .scan_cli import _render
from .scanner import scan_once


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass


async def _tick(venue, pair: str, tf: str, bars: int, store, user_id):
    try:
        result = await scan_once(
            venue, pair, interval=tf, bars=bars, store=store, user_id=user_id
        )
        print(_render(result), flush=True)
    except Exception as e:
        print(f"[ERROR {pair}] {type(e).__name__}: {e}", flush=True)


async def run(args: argparse.Namespace) -> int:
    store = None
    user_id = None
    if args.persist:
        if not args.dsn:
            print("--persist requires --dsn", file=sys.stderr)
            return 2
        from chalybcrypto_api.pg_store import PgStore  # noqa: WPS433

        store = PgStore(args.dsn)
        user_id = UUID(args.user)
        print(f"persist: enabled (dsn={args.dsn}, user={user_id})", flush=True)

    print(f"daemon: scanning {args.symbols} every {args.interval}s on {args.tf}", flush=True)

    stop_event = asyncio.Event()

    def _stop(*_a):
        stop_event.set()

    if hasattr(signal, "SIGINT"):
        try:
            asyncio.get_event_loop().add_signal_handler(signal.SIGINT, _stop)
            asyncio.get_event_loop().add_signal_handler(signal.SIGTERM, _stop)
        except NotImplementedError:
            # Windows: fall back to KeyboardInterrupt handling in the asyncio.run wrapper
            pass

    venue = BitunixConnector()
    try:
        while not stop_event.is_set():
            print(f"--- tick @ {asyncio.get_event_loop().time():.0f} ---", flush=True)
            await asyncio.gather(
                *(_tick(venue, sym, args.tf, args.bars, store, user_id) for sym in args.symbols),
                return_exceptions=False,
            )
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=args.interval)
            except asyncio.TimeoutError:
                pass
    finally:
        await venue.aclose()
        print("daemon: stopped cleanly", flush=True)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Continuous scanner daemon.")
    p.add_argument("symbols", nargs="+", help="trading pairs, e.g. BTCUSDT ETHUSDT")
    p.add_argument("--interval", type=float, default=30.0, help="seconds between ticks")
    p.add_argument("--tf", default="5m", help="kline timeframe")
    p.add_argument("--bars", type=int, default=300, help="kline lookback")
    p.add_argument("--persist", action="store_true",
                   help="persist every signal + decision into Postgres via PgStore")
    p.add_argument("--dsn", help="libpq DSN for --persist")
    p.add_argument("--user", default="11111111-1111-1111-1111-111111111111",
                   help="user UUID rows are persisted under")
    args = p.parse_args()
    # psycopg async on Windows needs SelectorEventLoop. Must be set BEFORE
    # asyncio.run() — once inside the loop, set_event_loop_policy is a no-op
    # for the current loop. See conftest.py for the same fix in tests.
    if args.persist and sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
