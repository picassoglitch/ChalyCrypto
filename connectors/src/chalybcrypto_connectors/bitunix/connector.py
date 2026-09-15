"""Bitunix futures connector.

Endpoints (futures, base https://fapi.bitunix.com):
  GET  /api/v1/futures/market/kline          public
  GET  /api/v1/futures/market/depth          public
  GET  /api/v1/futures/market/funding_rate   public
  GET  /api/v1/futures/account               authed
  GET  /api/v1/futures/position/get_pending_positions  authed
  POST /api/v1/futures/trade/place_order     authed
  POST /api/v1/futures/trade/cancel_orders   authed

Responses are wrapped as {code, msg, data}. code == 0 → success.

CLAUDE.md compliance:
  * Errors raise ConnectorError so the risk engine fails safe.
  * Idempotency-key passthrough via `clientId` for place_order.
  * Secrets never logged.
  * Async-only; no LLM calls anywhere.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

import httpx

from chalybcrypto_shared import (
    Kline,
    MarginType,
    OrderBookLevel,
    OrderBookSnapshot,
    OrderType,
    Side,
)

from ..base import (
    Balance,
    ConnectorError,
    ExchangeConnector,
    FundingInfo,
    OrderRequest,
    OrderResult,
    PositionInfo,
    PositionSide,
)
from .auth import build_signed_headers


BITUNIX_REST_BASE = "https://fapi.bitunix.com"


_INTERVAL_OK = {
    "1m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w", "1M",
}


class BitunixConnector(ExchangeConnector):
    exchange = "bitunix"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        base_url: str = BITUNIX_REST_BASE,
        client: httpx.AsyncClient | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(base_url=self._base_url, timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ──────────────────────────────────────────────────────────────────────
    # internals
    # ──────────────────────────────────────────────────────────────────────

    def _require_keys(self) -> tuple[str, str]:
        if not self._api_key or not self._api_secret:
            raise ConnectorError("bitunix authed call requires api_key + api_secret")
        return self._api_key, self._api_secret

    @staticmethod
    def _unwrap(payload: Mapping[str, Any]) -> Any:
        code = payload.get("code")
        if code != 0:
            raise ConnectorError(
                f"bitunix non-zero code: {code} msg={payload.get('msg')!r}",
                code=str(code) if code is not None else None,
            )
        return payload.get("data")

    async def _get_public(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            r = await self._client.get(path, params=params)
        except httpx.HTTPError as e:
            raise ConnectorError(f"bitunix GET {path} transport error: {e}") from e
        if r.status_code >= 400:
            raise ConnectorError(f"bitunix GET {path} -> {r.status_code}", status=r.status_code)
        return self._unwrap(r.json())

    async def _get_authed(self, path: str, params: dict[str, Any] | None = None) -> Any:
        key, secret = self._require_keys()
        headers = build_signed_headers(
            api_key=key, secret_key=secret, params=params or {}, body=None
        ).as_dict()
        try:
            r = await self._client.get(path, params=params, headers=headers)
        except httpx.HTTPError as e:
            raise ConnectorError(f"bitunix GET {path} transport error: {e}") from e
        if r.status_code >= 400:
            raise ConnectorError(f"bitunix GET {path} -> {r.status_code}", status=r.status_code)
        return self._unwrap(r.json())

    async def _post_authed(self, path: str, body: dict[str, Any]) -> Any:
        key, secret = self._require_keys()
        headers = build_signed_headers(
            api_key=key, secret_key=secret, params=None, body=body
        ).as_dict()
        try:
            r = await self._client.post(path, json=body, headers=headers)
        except httpx.HTTPError as e:
            raise ConnectorError(f"bitunix POST {path} transport error: {e}") from e
        if r.status_code >= 400:
            raise ConnectorError(f"bitunix POST {path} -> {r.status_code}", status=r.status_code)
        return self._unwrap(r.json())

    # ──────────────────────────────────────────────────────────────────────
    # market data (public)
    # ──────────────────────────────────────────────────────────────────────

    async def klines(
        self,
        pair: str,
        interval: str,
        *,
        limit: int = 100,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Kline]:
        if interval not in _INTERVAL_OK:
            raise ConnectorError(f"bitunix klines: unknown interval {interval!r}")
        params: dict[str, Any] = {"symbol": pair, "interval": interval, "limit": min(limit, 200)}
        if start is not None:
            params["startTime"] = int(start.timestamp() * 1000)
        if end is not None:
            params["endTime"] = int(end.timestamp() * 1000)
        data = await self._get_public("/api/v1/futures/market/kline", params)
        out: list[Kline] = []
        for row in data or []:
            open_ms = int(row["time"])
            out.append(
                Kline(
                    open_time=datetime.fromtimestamp(open_ms / 1000, tz=timezone.utc),
                    open=Decimal(str(row["open"])),
                    high=Decimal(str(row["high"])),
                    low=Decimal(str(row["low"])),
                    close=Decimal(str(row["close"])),
                    volume=Decimal(str(row["baseVol"])),
                    close_time=datetime.fromtimestamp(open_ms / 1000, tz=timezone.utc),
                )
            )
        return out

    async def order_book(self, pair: str, *, limit: int = 50) -> OrderBookSnapshot:
        # Bitunix accepts {1, 5, 15, 50, max}; clamp to nearest allowed value.
        allowed = (1, 5, 15, 50)
        chosen: str
        if limit >= 50:
            chosen = "50"
        else:
            chosen = str(min(allowed, key=lambda x: abs(x - limit)))
        data = await self._get_public(
            "/api/v1/futures/market/depth", {"symbol": pair, "limit": chosen}
        )
        bids = [OrderBookLevel(price=Decimal(p), size=Decimal(s)) for p, s in (data or {}).get("bids", [])]
        asks = [OrderBookLevel(price=Decimal(p), size=Decimal(s)) for p, s in (data or {}).get("asks", [])]
        return OrderBookSnapshot(
            exchange=self.exchange,
            pair=pair,
            taken_at=datetime.now(timezone.utc),
            bids=bids,
            asks=asks,
            is_native=True,
        )

    async def trading_pairs(self) -> list[dict[str, Any]]:
        """Public list of futures contracts available on Bitunix.

        Returns one dict per symbol with the fields the dashboard needs to build a
        market picker: symbol, base, quote, max leverage, and trading status.
        Data-only — no keys required. Callers decide which to surface (e.g. only
        symbolStatus == "OPEN" and isApiSupported for live order routing).
        """
        data = await self._get_public("/api/v1/futures/market/trading_pairs")
        out: list[dict[str, Any]] = []
        for row in data or []:
            out.append(
                {
                    "symbol": row["symbol"],
                    "base": row.get("base", ""),
                    "quote": row.get("quote", ""),
                    "max_leverage": int(row.get("maxLeverage", 0) or 0),
                    "status": str(row.get("symbolStatus", "")).upper(),
                    "api_supported": bool(row.get("isApiSupported", False)),
                }
            )
        return out

    async def funding(self, pair: str) -> FundingInfo:
        data = await self._get_public("/api/v1/futures/market/funding_rate", {"symbol": pair})
        if not data:
            raise ConnectorError(f"bitunix funding: empty response for {pair}")
        row = data[0] if isinstance(data, list) else data
        return FundingInfo(
            pair=row["symbol"],
            funding_rate=Decimal(str(row["fundingRate"])),
            funding_interval_hours=int(row.get("fundingInterval", 8)),
            next_funding_time=datetime.fromtimestamp(int(row["nextFundingTime"]) / 1000, tz=timezone.utc),
            mark_price=Decimal(str(row["markPrice"])),
            last_price=Decimal(str(row["lastPrice"])),
            max_funding_rate=Decimal(str(row["maxFundingRate"])) if row.get("maxFundingRate") is not None else None,
            min_funding_rate=Decimal(str(row["minFundingRate"])) if row.get("minFundingRate") is not None else None,
        )

    # ──────────────────────────────────────────────────────────────────────
    # account / positions (authed)
    # ──────────────────────────────────────────────────────────────────────

    async def balances(self, margin_coin: str) -> list[Balance]:
        data = await self._get_authed("/api/v1/futures/account", {"marginCoin": margin_coin})
        return [
            Balance(
                margin_coin=row["marginCoin"],
                available=Decimal(str(row.get("available", "0"))),
                frozen=Decimal(str(row.get("frozen", "0"))),
                margin_locked=Decimal(str(row.get("margin", "0"))),
                cross_unrealized_pnl=Decimal(str(row.get("crossUnrealizedPNL", "0"))),
                isolation_unrealized_pnl=Decimal(str(row.get("isolationUnrealizedPNL", "0"))),
            )
            for row in (data or [])
        ]

    async def positions(self, *, pair: str | None = None) -> list[PositionInfo]:
        params: dict[str, Any] = {}
        if pair is not None:
            params["symbol"] = pair
        data = await self._get_authed(
            "/api/v1/futures/position/get_pending_positions", params or None
        )
        rows = data if isinstance(data, list) else (data or {}).get("positionList", [])
        out: list[PositionInfo] = []
        for r in rows or []:
            side_raw = str(r.get("side", "")).upper()
            side = PositionSide.LONG if side_raw == "LONG" else (
                PositionSide.SHORT if side_raw == "SHORT" else PositionSide.FLAT
            )
            margin_type = (
                MarginType.CROSS
                if str(r.get("marginMode", "")).upper() == "CROSS"
                else MarginType.ISOLATED
            )
            liq_raw = r.get("liqPrice")
            liq = Decimal(str(liq_raw)) if liq_raw is not None else None
            # Per docs: liqPrice ≤ 0 means "position at low risk" — surface as None so the
            # risk engine doesn't treat 0 as a real liquidation level.
            if liq is not None and liq <= 0:
                liq = None
            out.append(
                PositionInfo(
                    exchange=self.exchange,
                    pair=r["symbol"],
                    side=side,
                    qty=Decimal(str(r.get("qty", "0"))),
                    avg_entry_price=Decimal(str(r.get("avgOpenPrice", "0"))),
                    leverage=Decimal(str(r.get("leverage", "1"))),
                    margin_type=margin_type,
                    liquidation_price=liq,
                    unrealized_pnl=Decimal(str(r.get("unrealizedPNL", "0"))),
                    realized_pnl=Decimal(str(r.get("realizedPNL", "0"))),
                    margin=Decimal(str(r["margin"])) if r.get("margin") is not None else None,
                    funding_paid=Decimal(str(r.get("funding", "0"))),
                    opened_at=(
                        datetime.fromtimestamp(int(r["ctime"]) / 1000, tz=timezone.utc)
                        if r.get("ctime")
                        else None
                    ),
                )
            )
        return out

    # ──────────────────────────────────────────────────────────────────────
    # trading (authed)
    # ──────────────────────────────────────────────────────────────────────

    async def place_order(self, req: OrderRequest) -> OrderResult:
        body: dict[str, Any] = {
            "symbol": req.pair,
            "qty": format(req.qty.normalize(), "f"),
            "side": "BUY" if req.side == Side.LONG else "SELL",
            "orderType": _ORDER_TYPE_MAP[req.order_type],
            "clientId": req.idempotency_key,
        }
        if req.order_type in (OrderType.LIMIT, OrderType.STOP_LIMIT, OrderType.REDUCE_ONLY_LIMIT):
            if req.price is None:
                raise ConnectorError("bitunix place_order: price required for LIMIT")
            body["price"] = format(req.price.normalize(), "f")
            body["effect"] = "GTC"
        if req.reduce_only or req.order_type in (
            OrderType.REDUCE_ONLY_LIMIT,
            OrderType.REDUCE_ONLY_MARKET,
        ):
            body["reduceOnly"] = True
        if req.take_profit_price is not None:
            body["tpPrice"] = format(req.take_profit_price.normalize(), "f")
            body["tpStopType"] = "LAST_PRICE"
            body["tpOrderType"] = "MARKET"
        if req.stop_loss_price is not None:
            body["slPrice"] = format(req.stop_loss_price.normalize(), "f")
            body["slStopType"] = "LAST_PRICE"
            body["slOrderType"] = "MARKET"

        data = await self._post_authed("/api/v1/futures/trade/place_order", body)
        if not isinstance(data, dict) or "orderId" not in data:
            raise ConnectorError(f"bitunix place_order: unexpected response {data!r}")
        return OrderResult(
            exchange_order_id=str(data["orderId"]),
            client_id=str(data.get("clientId", req.idempotency_key)),
            status="submitted",
            submitted_at=datetime.now(timezone.utc),
        )

    async def cancel_order(
        self, pair: str, *, order_id: str | None = None, client_id: str | None = None
    ) -> bool:
        if not order_id and not client_id:
            raise ConnectorError("bitunix cancel_order: need order_id or client_id")
        ref: dict[str, str] = {}
        if order_id:
            ref["orderId"] = order_id
        if client_id:
            ref["clientId"] = client_id
        body = {"symbol": pair, "orderList": [ref]}
        await self._post_authed("/api/v1/futures/trade/cancel_orders", body)
        return True


_ORDER_TYPE_MAP: dict[OrderType, str] = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    OrderType.STOP_MARKET: "MARKET",
    OrderType.STOP_LIMIT: "LIMIT",
    OrderType.REDUCE_ONLY_MARKET: "MARKET",
    OrderType.REDUCE_ONLY_LIMIT: "LIMIT",
}
