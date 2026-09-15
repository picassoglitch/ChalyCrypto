"""LBank Futures (CFD) public connector — narrow read-only surface.

Endpoints (futures, base https://lbkperp.lbank.com):
  GET  /cfd/openApi/v1/pub/marketOrder?symbol=X&depth=N    order book
  GET  /cfd/openApi/v1/pub/marketData?productGroup=X       ticker incl. funding

Deliberately NOT shipping yet (until docs are verified):
  * klines() — the REST kline path for futures isn't cleanly documented and there's a
    known broken-endpoint GitHub issue. ARCHITECTURE §0.3: "the fill-gating book is
    ALWAYS native" — we won't ship a guessed kline parser as a strategy data source.
  * authed endpoints (balances, positions, place/cancel) — signing scheme needs
    verification before risking real keys.

So this class does NOT implement ExchangeConnector. It's a public context source. The
full ExchangeConnector implementation lands once the missing pieces are confirmed.

Response wrapper: {data, error_code, msg, result, success}. success=true OR error_code=0
indicates ok.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

import httpx

from chalybcrypto_shared import OrderBookLevel, OrderBookSnapshot

from ..base import ConnectorError


LBANK_FUTURES_BASE = "https://lbkperp.lbank.com"


class LBankTicker:
    """Lightweight ticker container — declared as class to keep file shape uniform."""

    __slots__ = ("symbol", "last_price", "mark_price", "high", "low", "open", "volume", "funding_rate", "taken_at")

    def __init__(
        self,
        *,
        symbol: str,
        last_price: Decimal,
        mark_price: Decimal,
        high: Decimal,
        low: Decimal,
        open: Decimal,
        volume: Decimal,
        funding_rate: Decimal,
        taken_at: datetime,
    ) -> None:
        self.symbol = symbol
        self.last_price = last_price
        self.mark_price = mark_price
        self.high = high
        self.low = low
        self.open = open
        self.volume = volume
        self.funding_rate = funding_rate
        self.taken_at = taken_at

    def __repr__(self) -> str:
        return (
            f"LBankTicker(symbol={self.symbol!r}, last={self.last_price}, mark={self.mark_price}, "
            f"funding={self.funding_rate})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, LBankTicker):
            return NotImplemented
        return all(getattr(self, s) == getattr(other, s) for s in self.__slots__)


class LBankPublicConnector:
    source = "lbank_public"

    def __init__(
        self,
        *,
        base_url: str = LBANK_FUTURES_BASE,
        client: httpx.AsyncClient | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=self._base_url, timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _unwrap(payload: Mapping[str, Any]) -> Any:
        # LBank uses {data, error_code, msg, result, success}; data may itself be an array
        # or an object (or {symbol, asks, bids} unwrapped for order books, per the docs).
        if "error_code" in payload and payload.get("error_code") not in (0, "0"):
            raise ConnectorError(
                f"lbank error_code={payload.get('error_code')} msg={payload.get('msg')!r}",
                code=str(payload.get("error_code")),
            )
        if "success" in payload and payload["success"] is False:
            raise ConnectorError(
                f"lbank success=false msg={payload.get('msg')!r}",
                code=str(payload.get("error_code", "")),
            )
        return payload.get("data", payload)

    async def order_book(self, symbol: str, *, depth: int = 20) -> OrderBookSnapshot:
        # Per docs: each row is {price, volume, orders}.
        params = {"symbol": symbol, "depth": depth}
        try:
            r = await self._client.get("/cfd/openApi/v1/pub/marketOrder", params=params)
        except httpx.HTTPError as e:
            raise ConnectorError(f"lbank GET marketOrder transport error: {e}") from e
        if r.status_code >= 400:
            raise ConnectorError(
                f"lbank GET marketOrder -> {r.status_code}", status=r.status_code
            )
        data = self._unwrap(r.json())
        bids_raw = data.get("bids", []) or []
        asks_raw = data.get("asks", []) or []
        bids = [
            OrderBookLevel(price=Decimal(str(b["price"])), size=Decimal(str(b["volume"])))
            for b in bids_raw
        ]
        asks = [
            OrderBookLevel(price=Decimal(str(a["price"])), size=Decimal(str(a["volume"])))
            for a in asks_raw
        ]
        return OrderBookSnapshot(
            exchange="lbank",
            pair=str(data.get("symbol", symbol)),
            taken_at=datetime.now(timezone.utc),
            bids=bids,
            asks=asks,
            is_native=True,
        )

    async def market_data(self, product_group: str) -> list[LBankTicker]:
        """Return tickers for every symbol in a productGroup. funding_rate comes from
        the documented `prePositionFeeRate` field (the venue funding you would actually pay)."""
        try:
            r = await self._client.get(
                "/cfd/openApi/v1/pub/marketData",
                params={"productGroup": product_group},
            )
        except httpx.HTTPError as e:
            raise ConnectorError(f"lbank GET marketData transport error: {e}") from e
        if r.status_code >= 400:
            raise ConnectorError(
                f"lbank GET marketData -> {r.status_code}", status=r.status_code
            )
        data = self._unwrap(r.json())
        if not isinstance(data, list):
            raise ConnectorError(f"lbank marketData: unexpected payload type {type(data).__name__}")
        now = datetime.now(timezone.utc)
        out: list[LBankTicker] = []
        for row in data:
            out.append(
                LBankTicker(
                    symbol=str(row.get("symbol", "")),
                    last_price=Decimal(str(row.get("lastPrice", "0"))),
                    mark_price=Decimal(str(row.get("markedPrice", "0"))),
                    high=Decimal(str(row.get("highestPrice", "0"))),
                    low=Decimal(str(row.get("lowestPrice", "0"))),
                    open=Decimal(str(row.get("openPrice", "0"))),
                    volume=Decimal(str(row.get("volume", "0"))),
                    funding_rate=Decimal(str(row.get("prePositionFeeRate", "0"))),
                    taken_at=now,
                )
            )
        return out
