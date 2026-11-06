"""USD → MXN for Señales reference prices (hub TOOLS-SPEC Q5: prices in MXN).

Bitso's public USDT/MXN ticker (no key). Cached 10 minutes per process; None
when it can't be read, and callers then leave MXN prices out rather than guess.
"""

from __future__ import annotations

import time
from decimal import Decimal

import httpx

_URL = "https://api.bitso.com/v3/ticker/?book=usdt_mxn"
_TTL = 600
_cache: tuple[float, Decimal] | None = None


async def usd_mxn(client: httpx.AsyncClient | None = None) -> Decimal | None:
    global _cache
    if _cache and time.time() - _cache[0] < _TTL:
        return _cache[1]
    own = client is None
    c = client or httpx.AsyncClient(timeout=5)
    try:
        r = await c.get(_URL)
        rate = Decimal(str(r.json()["payload"]["last"]))
        if not (Decimal("5") < rate < Decimal("50")):  # sanity bounds
            return None
        _cache = (time.time(), rate)
        return rate
    except (httpx.HTTPError, KeyError, ValueError, TypeError, ArithmeticError):
        return None
    finally:
        if own:
            await c.aclose()


def reset_cache_for_tests() -> None:
    global _cache
    _cache = None
