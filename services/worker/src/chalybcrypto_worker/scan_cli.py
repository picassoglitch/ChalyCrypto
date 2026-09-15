"""Scanner CLI — run scan_once against live Bitunix on a tick interval.

  python -m chalybcrypto_worker.scan_cli BTCUSDT
  python -m chalybcrypto_worker.scan_cli BTCUSDT --interval 30 --ticks 5
  python -m chalybcrypto_worker.scan_cli ETHUSDT --bars 500 --tf 15m

  # Persist signals into Postgres (same DB the API runs against):
  python -m chalybcrypto_worker.scan_cli BTCUSDT --persist \
    --dsn postgresql://postgres@127.0.0.1:5432/chalybcrypto \
    --user 11111111-1111-1111-1111-111111111111

CLAUDE.md compliance: PAPER mode only, no order placement, deterministic decisions
printed with reasons. No LLM in the path.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from uuid import UUID

from chalybcrypto_connectors.bitunix import BitunixConnector

from .scanner import ScanResult, scan_once


try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass


def _render(result: ScanResult) -> str:
    lines: list[str] = []
    lines.append(
        f"[{result.taken_at.isoformat(timespec='seconds')}] {result.pair}  "
        f"mark={result.mark_price}  funding={result.funding_rate}  "
        f"bars={result.kline_count}"
    )
    for o in result.outcomes:
        if not o.fired:
            lines.append(f"  {o.strategy_key:<24}  no signal")
            continue
        sig = o.signal
        dec = o.decision
        verdict = "APPROVED" if dec and dec.approved else f"REJECTED ({dec.reason})"
        lines.append(
            f"  {o.strategy_key:<24}  {sig.side.value:<5} "
            f"entry={sig.entry} sl={sig.stop_loss} tp={sig.take_profits[0]}  -> {verdict}"
        )
    return "\n".join(lines)


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

    venue = BitunixConnector()
    try:
        for tick in range(1, args.ticks + 1):
            try:
                result = await scan_once(
                    venue, args.symbol, interval=args.tf, bars=args.bars,
                    store=store, user_id=user_id,
                )
                print(f"--- tick {tick}/{args.ticks} ---")
                print(_render(result), flush=True)
            except Exception as e:
                print(f"--- tick {tick}/{args.ticks} ERROR: {type(e).__name__}: {e} ---", flush=True)
            if tick < args.ticks:
                await asyncio.sleep(args.interval)
    finally:
        await venue.aclose()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Run the scanner against live Bitunix.")
    p.add_argument("symbol", help="trading pair, e.g. BTCUSDT")
    p.add_argument("--interval", type=float, default=30.0, help="seconds between ticks")
    p.add_argument("--ticks", type=int, default=1, help="number of ticks to run")
    p.add_argument("--tf", default="5m", help="kline timeframe")
    p.add_argument("--bars", type=int, default=300, help="kline lookback")
    p.add_argument("--persist", action="store_true",
                   help="persist every signal + decision into Postgres via PgStore")
    p.add_argument("--dsn", help="libpq DSN for --persist; e.g. postgresql://postgres@127.0.0.1:5432/chalybcrypto")
    p.add_argument("--user", default="11111111-1111-1111-1111-111111111111",
                   help="user UUID rows are persisted under (default: the demo user)")
    args = p.parse_args()
    # psycopg async on Windows needs SelectorEventLoop (Proactor is the 3.8+ default
    # and trips a clear error). Set the policy BEFORE asyncio.run() — once inside the
    # loop, the policy change is a no-op for the current loop.
    if args.persist and sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
