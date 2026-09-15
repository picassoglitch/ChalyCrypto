"""CoinMarketCap — MARKET CONTEXT only (ARCHITECTURE Lane C).

Use cases: ranking, market cap, 24h volume, trending. CLAUDE.md §0.3: CMC does NOT
provide futures funding, OI, or order book — it's a context source only, never used for
fill gating or trade triggers.

Endpoints used:
  GET /v1/cryptocurrency/listings/latest    top-N by market cap
  GET /v1/cryptocurrency/quotes/latest      quotes for specific symbols
  GET /v1/cryptocurrency/trending/latest    trending coins

Auth: X-CMC_PRO_API_KEY header.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import httpx

from ..base import ConnectorError


CMC_BASE = "https://pro-api.coinmarketcap.com"


@dataclass(frozen=True)
class CmcListing:
    rank: int
    symbol: str
    name: str
    market_cap_usd: Decimal
    volume_24h_usd: Decimal
    price_usd: Decimal
    percent_change_24h: Decimal
    last_updated: datetime


@dataclass(frozen=True)
class CmcQuote:
    symbol: str
    price_usd: Decimal
    volume_24h_usd: Decimal
    percent_change_24h: Decimal
    market_cap_usd: Decimal
    last_updated: datetime


def _parse_dt(s: str | None) -> datetime:
    if not s:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class CmcConnector:
    source = "coinmarketcap"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = CMC_BASE,
        client: httpx.AsyncClient | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=self._base, timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise ConnectorError("cmc: api_key required")
        return {"X-CMC_PRO_API_KEY": self._api_key, "Accept": "application/json"}

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            r = await self._client.get(path, params=params, headers=self._headers())
        except httpx.HTTPError as e:
            raise ConnectorError(f"cmc GET {path} transport error: {e}") from e
        if r.status_code >= 400:
            raise ConnectorError(f"cmc GET {path} -> {r.status_code}", status=r.status_code)
        payload = r.json()
        status = payload.get("status") or {}
        err = status.get("error_code")
        if err not in (0, None):
            raise ConnectorError(
                f"cmc error_code={err} msg={status.get('error_message')!r}", code=str(err),
            )
        return payload.get("data")

    async def listings_latest(self, *, limit: int = 100, convert: str = "USD") -> list[CmcListing]:
        data = await self._get(
            "/v1/cryptocurrency/listings/latest",
            {"start": 1, "limit": min(limit, 5000), "convert": convert},
        )
        out: list[CmcListing] = []
        for row in data or []:
            q = row.get("quote", {}).get(convert, {})
            out.append(
                CmcListing(
                    rank=int(row.get("cmc_rank", 0)),
                    symbol=str(row.get("symbol", "")),
                    name=str(row.get("name", "")),
                    market_cap_usd=Decimal(str(q.get("market_cap", "0"))),
                    volume_24h_usd=Decimal(str(q.get("volume_24h", "0"))),
                    price_usd=Decimal(str(q.get("price", "0"))),
                    percent_change_24h=Decimal(str(q.get("percent_change_24h", "0"))),
                    last_updated=_parse_dt(q.get("last_updated")),
                )
            )
        return out

    async def quotes_latest(self, symbols: list[str], *, convert: str = "USD") -> dict[str, CmcQuote]:
        if not symbols:
            return {}
        data = await self._get(
            "/v1/cryptocurrency/quotes/latest",
            {"symbol": ",".join(symbols), "convert": convert},
        )
        out: dict[str, CmcQuote] = {}
        for sym, rec in (data or {}).items():
            # quotes/latest may return either a dict or a list per-symbol depending on params
            row = rec[0] if isinstance(rec, list) else rec
            q = row.get("quote", {}).get(convert, {})
            out[sym] = CmcQuote(
                symbol=sym,
                price_usd=Decimal(str(q.get("price", "0"))),
                volume_24h_usd=Decimal(str(q.get("volume_24h", "0"))),
                percent_change_24h=Decimal(str(q.get("percent_change_24h", "0"))),
                market_cap_usd=Decimal(str(q.get("market_cap", "0"))),
                last_updated=_parse_dt(q.get("last_updated")),
            )
        return out

    async def trending_latest(self, *, limit: int = 20) -> list[str]:
        """Return the trending symbols, top first."""
        data = await self._get(
            "/v1/cryptocurrency/trending/latest", {"start": 1, "limit": limit}
        )
        return [str(row.get("symbol", "")) for row in (data or [])]
