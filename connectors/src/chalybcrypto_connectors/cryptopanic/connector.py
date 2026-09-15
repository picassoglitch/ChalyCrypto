"""CryptoPanic — crypto news aggregator (context only, never a trading signal).

The free tier requires an auth_token (obtained for free on cryptopanic.com).
Without a token, calls return 401/403 — surface as ConnectorError so the API
layer can return a structured "configure key" payload instead of a crash.

Endpoint:
  GET /api/v1/posts/?auth_token=...&currencies=BTC&filter=hot|rising|bullish|bearish

Returns a list of posts: each has title, url, source (domain + title),
created_at (ISO-8601), votes, and optional currencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from ..base import ConnectorError


CRYPTOPANIC_BASE = "https://cryptopanic.com"


@dataclass(frozen=True)
class NewsItem:
    published_at: datetime
    title: str
    url: str
    source: str
    currencies: tuple[str, ...]


def _parse_dt(s: str) -> datetime:
    # CryptoPanic returns "2026-06-07T15:42:11Z" or similar ISO-8601.
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


class CryptoPanicConnector:
    source = "cryptopanic"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = CRYPTOPANIC_BASE,
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

    async def posts(
        self,
        *,
        currencies: str | None = "BTC",
        filter_: str | None = None,
        public: bool = True,
        limit: int = 20,
    ) -> list[NewsItem]:
        if not self._api_key:
            raise ConnectorError("cryptopanic: api_key required")
        params: dict[str, Any] = {"auth_token": self._api_key}
        if currencies:
            params["currencies"] = currencies
        if filter_:
            params["filter"] = filter_
        if public:
            params["public"] = "true"
        try:
            r = await self._client.get("/api/v1/posts/", params=params)
        except httpx.HTTPError as e:
            raise ConnectorError(f"cryptopanic transport error: {e}") from e
        if r.status_code >= 400:
            raise ConnectorError(
                f"cryptopanic GET /api/v1/posts -> {r.status_code}", status=r.status_code
            )
        payload = r.json()
        results = payload.get("results") or []
        out: list[NewsItem] = []
        for row in results[:limit]:
            src = row.get("source") or {}
            currencies_field = row.get("currencies") or []
            out.append(
                NewsItem(
                    published_at=_parse_dt(row["published_at"]),
                    title=row.get("title", ""),
                    url=row.get("url", ""),
                    source=src.get("title") or src.get("domain") or "",
                    currencies=tuple(
                        c.get("code", "") for c in currencies_field if c.get("code")
                    ),
                )
            )
        return out
