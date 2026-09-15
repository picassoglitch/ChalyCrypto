"""One-shot Bitunix WS capture CLI.

Connects to the futures public WS, subscribes to one channel, prints the first N inbound
messages as pretty JSON, then exits. Used to verify the depth_books payload shape before
writing the decoder (the doc page is 404 as of 2026-06; CLAUDE.md §0.3 forbids gating fills
on a guessed shape).

Usage:
    python -m chalybcrypto_connectors.bitunix.capture BTCUSDT depth_books
    python -m chalybcrypto_connectors.bitunix.capture BTCUSDT tickers --count 5 --timeout 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .ws import BitunixPublicWS, Channel


async def capture(symbol: str, channel_name: str, count: int, timeout: float) -> int:
    async with BitunixPublicWS() as ws:
        await ws.subscribe(Channel(ch=channel_name, symbol=symbol))
        printed = 0

        async def _drain() -> None:
            nonlocal printed
            async for msg in ws.messages():
                print(json.dumps(msg, indent=2, sort_keys=True))
                print("---")
                printed += 1
                if printed >= count:
                    return

        try:
            await asyncio.wait_for(_drain(), timeout=timeout)
        except asyncio.TimeoutError:
            print(f"# timed out after {timeout}s with {printed} messages", file=sys.stderr)
            return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Capture raw Bitunix WS messages for one channel.")
    p.add_argument("symbol", help="e.g. BTCUSDT")
    p.add_argument("channel", help="e.g. depth_books, tickers")
    p.add_argument("--count", type=int, default=3, help="messages to print before exiting")
    p.add_argument("--timeout", type=float, default=30.0, help="seconds to wait overall")
    args = p.parse_args()
    return asyncio.run(capture(args.symbol, args.channel, args.count, args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
