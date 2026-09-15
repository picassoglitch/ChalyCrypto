"""API routes per BUILD_PLAN.

Permission model: every route requires `X-User-Id` (auth stub; Supabase JWT lands later).
Rejected/refused signals always carry the reason — CLAUDE.md "keep the UI honest".
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from chalybcrypto_shared import FeeSchedule, Mode, RiskProfile, vault_from_env

from .deps import get_current_user_id, get_store
from .store import ApiStore


router = APIRouter(prefix="/api")


_VALID_EXCHANGES = {"binance", "lbank", "bitunix"}


# ── signals ────────────────────────────────────────────────────────────────


@router.get("/signals")
async def list_signals(
    status_filter: str | None = Query(default=None, alias="status"),
    user_id: UUID = Depends(get_current_user_id),
    store: ApiStore = Depends(get_store),
) -> list[dict]:
    """Status can be one of: parsed | validated | rejected. None = all."""
    return await store.list_signals(user_id=user_id, status=status_filter)


# ── approvals (semi-auto queue) ────────────────────────────────────────────


class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str  # approve | reject | continue | close | breakeven | protect
    reason: str | None = None


_ALLOWED_APPROVAL_ACTIONS = {"approve", "reject", "continue", "close", "breakeven", "protect"}


@router.get("/approvals")
async def list_approvals(
    user_id: UUID = Depends(get_current_user_id),
    store: ApiStore = Depends(get_store),
) -> list[dict]:
    return await store.list_approvals(user_id=user_id)


@router.post("/approvals/{approval_id}/decision")
async def post_approval_decision(
    approval_id: UUID,
    body: ApprovalDecisionRequest,
    user_id: UUID = Depends(get_current_user_id),
    store: ApiStore = Depends(get_store),
) -> dict:
    if body.action not in _ALLOWED_APPROVAL_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown action {body.action!r}; allowed: {sorted(_ALLOWED_APPROVAL_ACTIONS)}",
        )

    # "approve" runs the execution coordinator: it resolves the approval and, when the
    # mode/paper-gate/connection conditions are all met, places the live order via the
    # deterministic ExecutionEngine (CLAUDE.md rules 3, 5, 8, 9). Every other action is a
    # plain state change.
    if body.action == "approve":
        approval = await store.get_approval(user_id=user_id, approval_id=approval_id)
        if approval is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="approval not found")
        if approval.get("status") not in (None, "pending"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"approval already {approval.get('status')}",
            )
        from .execution_coordinator import handle_approval_approve

        return await handle_approval_approve(store=store, user_id=user_id, approval=approval)

    result = await store.resolve_approval(
        user_id=user_id, approval_id=approval_id, action=body.action, reason=body.reason
    )
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="approval not found")
    return result


# ── connections (encrypted; secrets NEVER returned — CLAUDE.md rule 7) ─────


class ExchangeConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exchange: str
    api_key: str
    api_secret: str
    ip_allowlist: list[str] | None = None


@router.get("/connections/exchange")
async def list_exchange_connections_(
    user_id: UUID = Depends(get_current_user_id),
    store: ApiStore = Depends(get_store),
) -> list[dict]:
    """List the operator's exchange connections WITHOUT secrets."""
    return await store.list_exchange_connections(user_id=user_id)


@router.post("/connections/exchange", status_code=status.HTTP_201_CREATED)
async def add_exchange_connection_(
    body: ExchangeConnectionRequest,
    user_id: UUID = Depends(get_current_user_id),
    store: ApiStore = Depends(get_store),
) -> dict:
    """Store an encrypted exchange API key + secret. The plaintext NEVER touches
    the DB — it's encrypted via the SecretsVault (Fernet on
    CHALYBCRYPTO_MASTER_ENCRYPTION_KEY) before insert. The response NEVER echoes
    the plaintext back (CLAUDE.md rule 7)."""
    exchange = body.exchange.strip().lower()
    if exchange not in _VALID_EXCHANGES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown exchange {body.exchange!r}; allowed: {sorted(_VALID_EXCHANGES)}",
        )
    if not body.api_key or not body.api_secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="api_key and api_secret are required",
        )
    try:
        vault = vault_from_env()
    except RuntimeError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e)
        ) from e
    key_enc = vault.encrypt(body.api_key)
    secret_enc = vault.encrypt(body.api_secret)
    record = await store.add_exchange_connection(
        user_id=user_id,
        exchange=exchange,
        api_key_enc=key_enc,
        api_secret_enc=secret_enc,
        ip_allowlist=body.ip_allowlist,
    )
    # Defensive scrub — even if the store accidentally returned _enc bytes, drop them.
    return {
        k: v for k, v in record.items()
        if k not in ("api_key_enc", "api_secret_enc")
    }


# ── execution ──────────────────────────────────────────────────────────────


@router.get("/positions")
async def list_positions(
    user_id: UUID = Depends(get_current_user_id),
    store: ApiStore = Depends(get_store),
) -> list[dict]:
    return await store.list_positions(user_id=user_id)


@router.get("/trades")
async def list_trades(
    user_id: UUID = Depends(get_current_user_id),
    store: ApiStore = Depends(get_store),
) -> list[dict]:
    return await store.list_trades(user_id=user_id)


# ── mode (paper-gate enforced; full_auto disabled in MVP) ──────────────────


class ModePutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Mode


@router.get("/mode")
async def get_mode(
    user_id: UUID = Depends(get_current_user_id),
    store: ApiStore = Depends(get_store),
) -> dict:
    return await store.get_mode(user_id=user_id)


@router.put("/mode")
async def put_mode(
    body: ModePutRequest,
    user_id: UUID = Depends(get_current_user_id),
    store: ApiStore = Depends(get_store),
) -> dict:
    try:
        return await store.set_mode(user_id=user_id, mode=body.mode)
    except PermissionError as e:
        # CLAUDE.md rule 5: paper-before-live enforced in code + DB.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=str(e),
        ) from e


# ── risk profiles ──────────────────────────────────────────────────────────


@router.get("/risk-profiles")
async def get_risk_profile_(
    user_id: UUID = Depends(get_current_user_id),
    store: ApiStore = Depends(get_store),
) -> dict | None:
    p = await store.get_risk_profile(user_id=user_id)
    return p.model_dump(mode="json") if p else None


@router.put("/risk-profiles")
async def put_risk_profile_(
    profile: RiskProfile,
    user_id: UUID = Depends(get_current_user_id),
    store: ApiStore = Depends(get_store),
) -> dict:
    saved = await store.put_risk_profile(user_id=user_id, profile=profile)
    return saved.model_dump(mode="json")


# ── fees (global; CLAUDE.md rule 6: source of truth is the table, never hardcoded) ──


@router.get("/fee-schedules")
async def list_fee_schedules(
    store: ApiStore = Depends(get_store),
) -> list[dict]:
    schedules = await store.list_fee_schedules()
    return [s.model_dump(mode="json") for s in schedules]


@router.put("/fee-schedules")
async def put_fee_schedules(
    schedules: list[FeeSchedule],
    store: ApiStore = Depends(get_store),
    _user_id: UUID = Depends(get_current_user_id),
) -> list[dict]:
    saved = await store.put_fee_schedules(schedules=schedules)
    return [s.model_dump(mode="json") for s in saved]


# ── strategies ─────────────────────────────────────────────────────────────


@router.get("/strategies")
async def list_strategies(
    store: ApiStore = Depends(get_store),
    _user_id: UUID = Depends(get_current_user_id),
) -> list[dict]:
    return await store.list_strategies()


# ── backtests (queue stub; runner integration in Phase 4 backtester) ──────


class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy: str
    pair: str
    timeframe: str = "5m"
    bars: int = 1500


@router.post("/backtests", status_code=status.HTTP_202_ACCEPTED)
async def queue_backtest(
    body: BacktestRequest,
    _user_id: UUID = Depends(get_current_user_id),
) -> dict:
    # The actual job is enqueued onto Celery in Phase 6 worker integration.
    # For now we return the job description so the dashboard can show 'queued'.
    return {
        "status": "queued",
        "strategy": body.strategy,
        "pair": body.pair,
        "timeframe": body.timeframe,
        "bars": body.bars,
        "optimistic": True,  # CLAUDE.md: backtests are always labelled OPTIMISTIC
    }


# ── SSE telemetry (skeleton — real subscription is wired in Phase 6 worker) ──


@router.get("/klines/{pair}")
async def proxy_klines(
    pair: str,
    interval: str = Query(default="5m"),
    bars: int = Query(default=200, ge=10, le=1500),
    _user_id: UUID = Depends(get_current_user_id),
) -> list[dict]:
    """Proxy Bitunix public klines so the dashboard chart doesn't need CORS or keys.

    Returns rows shaped {time, open, high, low, close} ready for Lightweight Charts.
    """
    from chalybcrypto_connectors.bitunix import BitunixConnector  # local to keep startup light

    venue = BitunixConnector()
    try:
        rows = await venue.klines(pair, interval, limit=bars)
    finally:
        await venue.aclose()
    return [
        {
            "time": int(k.open_time.timestamp()),
            "open": float(k.open),
            "high": float(k.high),
            "low": float(k.low),
            "close": float(k.close),
        }
        for k in rows
    ]


# Venue capabilities — where bots may run. Mirrors CLAUDE.md:
#   Bitunix = primary live futures venue (built first); LBank = second live venue;
#   Binance = data-only (free historical klines for backtests, NEVER live trading).
_VENUES: list[dict[str, Any]] = [
    {
        "id": "bitunix",
        "name": "Bitunix",
        "live": True,
        "data_only": False,
        "note": "Venue de fill nativo · order book en vivo · ejecución",
    },
    {
        "id": "lbank",
        "name": "LBank",
        "live": True,
        "data_only": False,
        "note": "Segundo venue de fill (en construcción)",
    },
    {
        "id": "binance",
        "name": "Binance",
        "live": False,
        "data_only": True,
        "note": "Solo datos · klines históricos para backtests · sin trading en vivo",
    },
]


@router.get("/markets")
async def list_markets(
    venue: str = Query(default="bitunix"),
    _user_id: UUID = Depends(get_current_user_id),
) -> dict:
    """Available venues (where bots can run) + the tradeable futures symbols for one.

    `venues` lists every connector and whether it supports live trading or is
    data-only (CLAUDE.md: Binance is data-only). `symbols` is the live contract
    list for the requested `venue`. Only Bitunix exposes a public contract list
    today; other venues return an empty symbol list with their capabilities still
    described so the UI can show where bots may eventually run.
    """
    venue = venue.strip().lower()
    if venue not in _VALID_EXCHANGES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown venue {venue!r}; allowed: {sorted(_VALID_EXCHANGES)}",
        )

    symbols: list[dict] = []
    error: str | None = None
    if venue == "bitunix":
        from chalybcrypto_connectors.bitunix import BitunixConnector

        conn = BitunixConnector()
        try:
            pairs = await conn.trading_pairs()
        except Exception as e:  # surface, never crash the dashboard
            error = str(e)
            pairs = []
        finally:
            await conn.aclose()
        # Only OPEN, API-routable contracts can host a live bot. Sort majors first.
        _MAJORS = {"BTC": 0, "ETH": 1, "SOL": 2, "BNB": 3, "XRP": 4}
        usable = [p for p in pairs if p["status"] == "OPEN" and p["api_supported"]]
        usable.sort(key=lambda p: (_MAJORS.get(p["base"], 99), p["symbol"]))
        symbols = usable

    return {
        "venues": _VENUES,
        "active_venue": venue,
        "symbols": symbols,
        "count": len(symbols),
        "error": error,
    }


@router.get("/indicators/{pair}")
async def proxy_indicators(
    pair: str,
    interval: str = Query(default="5m"),
    bars: int = Query(default=300, ge=60, le=1500),
    _user_id: UUID = Depends(get_current_user_id),
) -> dict:
    """Klines + RSI(14) + MACD(12/26/9) + ADX(14) for the dashboard chart.

    Returns:
      {
        "candles": [{time, open, high, low, close}, ...],   # ascending by time
        "rsi":     [{time, value}, ...],
        "macd":    {"line": [...], "signal": [...], "hist": [...]},
        "adx":     [{time, value}, ...],
      }
    Series are sparse (entries omitted during the indicator's warmup window).
    """
    from chalybcrypto_connectors.bitunix import BitunixConnector
    from chalybcrypto_engine.strategy.indicators import adx, macd, rsi

    venue = BitunixConnector()
    try:
        klines = await venue.klines(pair, interval, limit=bars)
    finally:
        await venue.aclose()

    times = [int(k.open_time.timestamp()) for k in klines]
    candles = [
        {
            "time": times[i],
            "open": float(klines[i].open),
            "high": float(klines[i].high),
            "low": float(klines[i].low),
            "close": float(klines[i].close),
        }
        for i in range(len(klines))
    ]

    rsi_vals = rsi(klines, period=14)
    adx_vals = adx(klines, period=14)
    macd_line, macd_signal, macd_hist = macd(klines, fast=12, slow=26, signal=9)

    # Approximate volume delta from candle direction. Without trade-by-trade data
    # this is the standard fallback Coinglass uses on lower-resolution streams.
    volume_delta = []
    for k in klines:
        direction = 1 if k.close > k.open else (-1 if k.close < k.open else 0)
        volume_delta.append(float(k.volume) * direction)

    def _sparse(values: list) -> list[dict]:
        return [
            {"time": times[i], "value": float(v)}
            for i, v in enumerate(values)
            if v is not None
        ]

    return {
        "candles": candles,
        "rsi": _sparse(rsi_vals),
        "macd": {
            "line": _sparse(macd_line),
            "signal": _sparse(macd_signal),
            "hist": _sparse(macd_hist),
        },
        "adx": _sparse(adx_vals),
        "volume_delta": [
            {"time": times[i], "value": v} for i, v in enumerate(volume_delta)
        ],
    }


@router.get("/coinglass/{symbol}")
async def coinglass_context(
    symbol: str,
    interval: str = Query(default="1h"),
    exchange: str = Query(default="Binance"),
    _user_id: UUID = Depends(get_current_user_id),
) -> dict:
    """Coinglass derivatives context: OI series, OI-weighted funding, aggregated
    liquidations. **Context only** — CLAUDE.md §0.3 forbids using Coinglass data
    for fill gating (snapshots are ≤1-min, not execution-grade).

    Normalizes the user-facing pair (e.g. BTCUSDT) to Coinglass's base-coin
    convention (BTC). Returns 503 with a structured payload if the API key isn't
    configured so the dashboard can show a graceful empty state.
    """
    import os

    api_key = os.environ.get("COINGLASS_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail={"error": "coinglass_api_key_missing", "symbol": symbol},
        )

    base = symbol.upper()
    for suffix in ("USDT", "USDC", "USD"):
        if base.endswith(suffix) and len(base) > len(suffix):
            base = base[: -len(suffix)]
            break

    from chalybcrypto_connectors import CoinglassConnector, ConnectorError

    venue = CoinglassConnector(api_key=api_key)
    try:
        oi = await venue.open_interest_history(base, interval=interval, exchange=exchange)
        funding = await venue.funding_oi_weighted_history(base, interval=interval)
        liqs = await venue.liquidations_aggregated_history(base, interval=interval)
        # Long/short ratio + heatmap are higher-tier endpoints — degrade
        # gracefully so the panel still loads if the plan can't reach them.
        try:
            ratio = await venue.long_short_ratio_history(
                base, interval=interval, exchange=exchange
            )
        except ConnectorError:
            ratio = []
        try:
            heatmap = await venue.liquidation_heatmap(
                base, interval="12h", exchange=exchange
            )
        except ConnectorError:
            heatmap = []
    finally:
        await venue.aclose()

    return {
        "symbol": base,
        "interval": interval,
        "oi": [
            {"time": int(r.taken_at.timestamp()), "value": float(r.close)} for r in oi
        ],
        "funding": [
            {"time": int(r.taken_at.timestamp()), "value": float(r.weighted_rate)}
            for r in funding
        ],
        "liquidations": [
            {
                "time": int(r.taken_at.timestamp()),
                "long_usd": float(r.long_liq_usd),
                "short_usd": float(r.short_liq_usd),
            }
            for r in liqs
        ],
        "long_short": [
            {
                "time": int(r.taken_at.timestamp()),
                "long_pct": float(r.long_pct),
                "short_pct": float(r.short_pct),
            }
            for r in ratio
        ],
        "heatmap": [
            {"price": float(c.price), "amount_usd": float(c.leverage_amount_usd)}
            for c in heatmap
        ],
    }


@router.get("/orderbook/{pair}")
async def proxy_orderbook(
    pair: str,
    limit: int = Query(default=50, ge=5, le=50),
    _user_id: UUID = Depends(get_current_user_id),
) -> dict:
    """Bitunix native order book — the fill-gating book per CLAUDE.md §0.3.

    Returned top-of-book is sorted: bids descending, asks ascending. Each level
    includes a cumulative size so the dashboard can render depth shading without
    recomputing on every render.
    """
    from chalybcrypto_connectors.bitunix import BitunixConnector

    venue = BitunixConnector()
    try:
        snap = await venue.order_book(pair, limit=limit)
    finally:
        await venue.aclose()

    bids = sorted(snap.bids, key=lambda lvl: lvl.price, reverse=True)
    asks = sorted(snap.asks, key=lambda lvl: lvl.price)

    def _enrich(levels) -> list[dict]:
        cum = 0.0
        out: list[dict] = []
        for lvl in levels:
            cum += float(lvl.size)
            out.append(
                {"price": float(lvl.price), "size": float(lvl.size), "cumulative": cum}
            )
        return out

    return {
        "pair": snap.pair,
        "taken_at": snap.taken_at.isoformat(),
        "is_native": snap.is_native,
        "bids": _enrich(bids),
        "asks": _enrich(asks),
    }


@router.get("/news")
async def proxy_news(
    currencies: str = Query(default="BTC"),
    limit: int = Query(default=20, ge=1, le=50),
    _user_id: UUID = Depends(get_current_user_id),
) -> dict:
    """CryptoPanic news feed — context only, never a trading signal.

    Returns 503 with structured payload when CRYPTOPANIC_API_KEY is unset so the
    dashboard renders a graceful empty state instead of failing.
    """
    import os

    api_key = os.environ.get("CRYPTOPANIC_API_KEY")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail={"error": "cryptopanic_api_key_missing", "currencies": currencies},
        )

    from chalybcrypto_connectors import CryptoPanicConnector

    feed = CryptoPanicConnector(api_key=api_key)
    try:
        items = await feed.posts(currencies=currencies, limit=limit)
    finally:
        await feed.aclose()

    return {
        "currencies": currencies,
        "items": [
            {
                "published_at": item.published_at.isoformat(),
                "title": item.title,
                "url": item.url,
                "source": item.source,
                "currencies": list(item.currencies),
            }
            for item in items
        ],
    }


@router.get("/stream")
async def stream(
    _user_id: UUID = Depends(get_current_user_id),
) -> StreamingResponse:
    """SSE skeleton — yields a single 'connected' event then idles. Real fan-out from
    the worker happens via Redis pub/sub in the next iteration."""

    async def gen():
        yield b"event: ready\ndata: {\"ok\": true}\n\n"
        # The route exits immediately for now; real impl loops on the Redis sub.
        await asyncio.sleep(0)

    return StreamingResponse(gen(), media_type="text/event-stream")
