"""Binance USDⓈ-M Futures public market data — KLINES ONLY.

Per CLAUDE.md and ARCHITECTURE §0.3, Binance is DROPPED as a trading venue. It is kept
only as a free historical-kline source for backtests. This class deliberately does NOT
implement ExchangeConnector and does NOT expose order placement, balances, or auth — the
type system itself enforces "data-only". If you ever feel tempted to add place_order
here, re-read §0.3 and stop.

Endpoint: GET https://fapi.binance.com/fapi/v1/klines (public, no keys, no KYC).
Docs: https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Kline-Candlestick-Data
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

from chalybcrypto_shared import Kline

from ..base import ConnectorError


BINANCE_FUTURES_DATA_BASE = "https://fapi.binance.com"

_INTERVAL_OK = {
    "1m", "3m", "5m", "15m", "30m",
    "1h", "2h", "4h", "6h", "8h", "12h",
    "1d", "3d", "1w", "1M",
}


class BinanceDataConnector:
    """Read-only kline fetcher. Intentionally narrow."""

    source = "binance_futures_data"

    def __init__(
        self,
        *,
        base_url: str = BINANCE_FUTURES_DATA_BASE,
        client: httpx.AsyncClient | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=self._base_url, timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def klines(
        self,
        pair: str,
        interval: str,
        *,
        limit: int = 500,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Kline]:
        if interval not in _INTERVAL_OK:
            raise ConnectorError(f"binance klines: unknown interval {interval!r}")
        params: dict[str, Any] = {"symbol": pair, "interval": interval, "limit": min(limit, 1500)}
        if start is not None:
            params["startTime"] = int(start.timestamp() * 1000)
        if end is not None:
            params["endTime"] = int(end.timestamp() * 1000)

        try:
            r = await self._client.get("/fapi/v1/klines", params=params)
        except httpx.HTTPError as e:
            raise ConnectorError(f"binance klines transport error: {e}") from e
        if r.status_code >= 400:
            raise ConnectorError(
                f"binance klines -> {r.status_code}", status=r.status_code
            )

        rows = r.json()
        if not isinstance(rows, list):
            raise ConnectorError(f"binance klines: unexpected payload {rows!r}")

        out: list[Kline] = []
        for row in rows:
            # Field order: open_time, open, high, low, close, volume, close_time, ...
            out.append(
                Kline(
                    open_time=datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc),
                    open=Decimal(str(row[1])),
                    high=Decimal(str(row[2])),
                    low=Decimal(str(row[3])),
                    close=Decimal(str(row[4])),
                    volume=Decimal(str(row[5])),
                    close_time=datetime.fromtimestamp(int(row[6]) / 1000, tz=timezone.utc),
                )
            )
        return out
