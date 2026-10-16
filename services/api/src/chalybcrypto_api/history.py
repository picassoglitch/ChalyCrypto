"""Historical klines for backtests, oldest first.

Binance USDⓈ-M public klines are the designated backtest source (CLAUDE.md §0.3:
data only, never trading). Binance refuses some regions (HTTP 451, e.g. US
egress), so when it fails the same window comes from Bitunix, the live venue.
The result says which source was used.
"""

from __future__ import annotations

from datetime import timedelta

from chalybcrypto_connectors import BinanceDataConnector, ConnectorError
from chalybcrypto_connectors.bitunix import BitunixConnector
from chalybcrypto_shared import Kline


class HistoryUnavailable(Exception):
    pass


async def _paginate(conn, pair: str, interval: str, bars: int, page: int) -> list[Kline]:
    out: list[Kline] = []
    end = None
    while len(out) < bars:
        want = min(page, bars - len(out))
        rows = await conn.klines(pair, interval, limit=want, end=end)
        rows = sorted(rows, key=lambda k: k.open_time)
        if out:
            rows = [k for k in rows if k.open_time < out[0].open_time]
        if not rows:
            break
        out = rows + out
        end = rows[0].open_time - timedelta(milliseconds=1)
        if len(rows) < want:
            break
    return out[-bars:]


async def fetch_history(pair: str, interval: str, bars: int) -> tuple[list[Kline], str]:
    errors: list[str] = []
    for name, make, page in (
        ("binance", BinanceDataConnector, 1500),
        ("bitunix", BitunixConnector, 200),
    ):
        conn = make()
        try:
            rows = await _paginate(conn, pair, interval, bars, page)
            if len(rows) >= 50:
                return rows, name
            errors.append(f"{name}: {len(rows)} bars")
        except ConnectorError as e:
            errors.append(f"{name}: {e}")
        finally:
            await conn.aclose()
    raise HistoryUnavailable("; ".join(errors))
