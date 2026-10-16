"""/api/hub/v1 — the JSON API the Chalyb hub's server calls (Señales in the hub).

Contract (versioned; additive changes only within v1):

  Auth     Authorization: Bearer <hub token> on every route (hub_auth.py).
           Nothing else is accepted here: no cookie, no X-User-Id, no Supabase JWT.
  Routes   Every /api/<route> of routes.py, same handlers and same business
           rules, at /api/hub/v1/<route> — except:
             PUT /fee-schedules  global table, not a per-user action
             GET /stream         SSE skeleton, no data yet
           plus the hub-only routes below:
             GET /me                     who the token is for
             GET /signal-prefs           the person's Señales delivery filter (or null)
             PUT /signal-prefs           replace it (validated, extra fields refused)
             GET /feed?plan=&since=      the Señales shared feed, newest first
             GET /feed/{id}?range=       one feed entry + its past price (MXN)
  Errors   FastAPI's {"detail": ...}. Usage refusals keep the consumption
           contract: 402/429/413 with detail {error: "usage_refused", reason, ...};
           hub unreachable: 503 {error: "hub_unavailable"}.

The routes are copied from routes.router at import time, so a new /api route
shows up here too unless it is added to _EXCLUDED.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .auth import current_user_id
from .deps import get_store
from .feed import wire as feed_wire
from .hub_auth import HubPrincipal, hub_principal, hub_user_id
from .routes import router as api_router
from .store import ApiStore

HUB_API_PREFIX = "/api/hub/v1"

_EXCLUDED: set[tuple[str, str]] = {
    ("/fee-schedules", "PUT"),
    ("/stream", "GET"),
}


def _engine_routes() -> APIRouter:
    out = APIRouter()
    for route in api_router.routes:
        if not isinstance(route, APIRoute):
            continue
        path = route.path.removeprefix("/api")
        methods = sorted(m for m in route.methods if (path, m) not in _EXCLUDED)
        if not methods:
            continue
        out.add_api_route(
            path,
            route.endpoint,
            methods=methods,
            status_code=route.status_code,
            response_class=route.response_class,
            name=route.name,
            dependencies=route.dependencies,
        )
    return out


# ── hub-only routes ────────────────────────────────────────────────────────

_Coin = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9]{1,15}$")]
_HHMM = Annotated[str, StringConstraints(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]


class SignalPrefs(BaseModel):
    """Mirror of the hub's SignalPrefs (src/lib/tools/adapters/tools.ts).
    Delivery only: coins FILTER what arrives; nothing here can change a signal."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    coins: list[_Coin] = Field(max_length=50)
    channels: list[Literal["whatsapp", "email", "app"]] = Field(max_length=3)
    timeframe: Literal["day", "week"]
    quietHours: bool
    from_: _HHMM | None = Field(default=None, alias="from")
    to: _HHMM | None = None
    days: Literal["all", "weekdays"] | None = None
    dailySummary: bool | None = None
    format: Literal["short", "explained"] | None = None

    def wire(self) -> dict:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


hub_router = APIRouter()


@hub_router.get("/me")
async def me(p: HubPrincipal = Depends(hub_principal)) -> dict:
    return {"user_id": str(p.user_id), "email": p.email, "tier": p.tier}


@hub_router.get("/signal-prefs")
async def get_signal_prefs(
    user_id: UUID = Depends(hub_user_id),
    store: ApiStore = Depends(get_store),
) -> dict | None:
    raw = await store.get_signal_prefs(user_id=user_id)
    if raw is None:
        return None
    # A row written by an older shape is still served, minus unknown fields.
    try:
        return SignalPrefs.model_validate(raw).wire()
    except ValueError:
        return None


@hub_router.put("/signal-prefs")
async def put_signal_prefs(
    body: SignalPrefs,
    user_id: UUID = Depends(hub_user_id),
    store: ApiStore = Depends(get_store),
) -> dict:
    # De-duplicate, keep order.
    body.coins = list(dict.fromkeys(body.coins))
    body.channels = list(dict.fromkeys(body.channels))
    saved = await store.put_signal_prefs(user_id=user_id, prefs=body.wire())
    return SignalPrefs.model_validate(saved).wire()


# ── Señales shared feed (the same for every user and plan) ─────────────────

_RANGES = {"1d": ("1h", 24), "7d": ("4h", 42), "1m": ("1d", 30)}


@hub_router.get("/feed")
async def get_feed(
    plan: str = Query(default="PRO", pattern=r"^(FREE|PRO|VIP)$"),
    since: datetime | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    _user_id: UUID = Depends(hub_user_id),
    store: ApiStore = Depends(get_store),
) -> dict:
    """`plan` is accepted for the contract but does not change the content:
    today every plan sees the same feed. Default window: the last 30 days."""
    since = since or datetime.now(UTC) - timedelta(days=30)
    rows = await store.list_feed(since=since, limit=limit)
    return {"plan": plan, "signals": [feed_wire(r) for r in rows]}


@hub_router.get("/feed/{entry_id}")
async def get_feed_entry(
    entry_id: UUID,
    range_: str = Query(default="7d", alias="range", pattern=r"^(1d|7d|1m)$"),
    _user_id: UUID = Depends(hub_user_id),
    store: ApiStore = Depends(get_store),
) -> dict:
    """One entry and the coin's PAST price up to now (no projection), in MXN.
    `series` is empty when the MXN rate or the price source is unavailable."""
    row = await store.get_feed_entry(entry_id=entry_id)
    if row is None:
        raise HTTPException(status_code=404, detail="feed entry not found")
    from chalybcrypto_connectors import ConnectorError
    from chalybcrypto_connectors.bitunix import BitunixConnector

    from .fx import usd_mxn

    interval, n = _RANGES[range_]
    series: list[dict] = []
    rate = await usd_mxn()
    if rate is not None:
        conn = BitunixConnector()
        try:
            klines = await conn.klines(row["pair"], interval, limit=n)
            series = [
                {"at": k.open_time.isoformat(), "price_mxn": float(k.close * rate)}
                for k in sorted(klines, key=lambda k: k.open_time)
            ]
        except ConnectorError:
            series = []
        finally:
            await conn.aclose()
    return {"signal": feed_wire(row), "series": series}


def build_hub_app() -> FastAPI:
    # No docs/openapi on the hub app: it is a server-to-server surface.
    hub_app = FastAPI(
        title="ChalyCrypto hub API v1",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    hub_app.include_router(hub_router)
    hub_app.include_router(_engine_routes())
    # Every engine route resolves its user from the hub token instead.
    hub_app.dependency_overrides[current_user_id] = hub_user_id
    return hub_app
