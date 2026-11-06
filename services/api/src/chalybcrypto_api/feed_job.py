"""Señales feed job: one tick over every coin, then exit.

    python -m chalybcrypto_api.feed_job

Runs as the Cloud Run Job `chalybcrypto-feed` (hub infra/terraform, engines
map → chalybcrypto.jobs.feed) on a Cloud Scheduler trigger every 15 minutes,
with the API's image and secrets. Needs CHALYBCRYPTO_DATABASE_URL or
DATABASE_URL (the store is always Postgres here: an in-memory feed would be
lost at exit). Exit 1 when every coin failed, so the scheduler shows it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

from chalybcrypto_connectors.bitunix import BitunixConnector

from .feed import run_feed_tick
from .fx import usd_mxn
from .pg_store import PgStore


async def main() -> int:
    logging.basicConfig(level=logging.INFO)
    dsn = os.environ.get("CHALYBCRYPTO_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("feed_job: CHALYBCRYPTO_DATABASE_URL or DATABASE_URL is required", file=sys.stderr)
        return 2
    store = PgStore(dsn)
    source = BitunixConnector()
    try:
        rate = await usd_mxn()
        result = await run_feed_tick(store, source, usd_mxn=rate)
    finally:
        await source.aclose()
    print(json.dumps({"feed_tick": result, "usd_mxn": str(rate) if rate else None}), flush=True)
    return 1 if result and all(v == "error" for v in result.values()) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
