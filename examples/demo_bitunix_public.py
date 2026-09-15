"""Live Bitunix REST: hit the three public endpoints (no keys needed) and print results."""

from __future__ import annotations

import asyncio
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

from chalybcrypto_connectors.bitunix import BitunixConnector


async def main() -> None:
    c = BitunixConnector()
    try:
        funding = await c.funding("BTCUSDT")
        print(f"funding   BTCUSDT  rate={funding.funding_rate}  "
              f"mark={funding.mark_price}  next={funding.next_funding_time.isoformat()}")

        book = await c.order_book("BTCUSDT", limit=5)
        best_ask = book.asks[0]
        best_bid = book.bids[0]
        print(f"order_book BTCUSDT  ask={best_ask.price}x{best_ask.size}  "
              f"bid={best_bid.price}x{best_bid.size}  spread={best_ask.price - best_bid.price}")

        klines = await c.klines("BTCUSDT", "5m", limit=3)
        for k in klines:
            print(f"kline 5m  {k.open_time.isoformat()}  "
                  f"o={k.open} h={k.high} l={k.low} c={k.close} v={k.volume}")
    finally:
        await c.aclose()


if __name__ == "__main__":
    asyncio.run(main())
