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
  Errors   FastAPI's {"detail": ...}. Usage refusals keep the consumption
           contract: 402/429/413 with detail {error: "usage_refused", reason, ...};
           hub unreachable: 503 {error: "hub_unavailable"}.

The routes are copied from routes.router at import time, so a new /api route
shows up here too unless it is added to _EXCLUDED.
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .auth import current_user_id
from .deps import get_store
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
