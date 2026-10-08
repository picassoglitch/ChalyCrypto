from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Cookie, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from chalybcrypto_shared import get_settings

from chalybcrypto_hub import run_drain_loop

from .admin import admin_router
from .auth import auth_mode
from .consumption import get_hub_client
from .deps import get_store
from .routes import router as api_router
from .sso import SESSION_COOKIE_NAME, sso_router, verify_session_jwt


def _cors_origins() -> list[str]:
    """Pulled from CHALYBCRYPTO_CORS_ORIGINS (csv). Default = localhost-only so dev
    works out of the box but production has to opt in explicitly."""
    raw = os.environ.get(
        "CHALYBCRYPTO_CORS_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000",
    )
    return [o.strip() for o in raw.split(",") if o.strip()]


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Drain the usage outbox to the Chalyb hub in the background. Only when the
    hub is configured (CHALYB_BASE_URL) and the store has an outbox; set
    CHALYBCRYPTO_USAGE_DRAIN=0 to run the drainer elsewhere (e.g. the worker)."""
    hub = get_hub_client()
    store = get_store()
    task: asyncio.Task | None = None
    stop = asyncio.Event()
    enabled = os.environ.get("CHALYBCRYPTO_USAGE_DRAIN", "1").strip() not in ("0", "false", "no")
    if enabled and hub is not None and hasattr(store, "claim_usage_outbox"):
        interval = float(os.environ.get("CHALYBCRYPTO_USAGE_DRAIN_INTERVAL", "15"))
        task = asyncio.create_task(
            run_drain_loop(store, hub, interval_seconds=interval, stop=stop)  # type: ignore[arg-type]
        )
    try:
        yield
    finally:
        if task is not None:
            stop.set()
            await task


app = FastAPI(title="ChalyCrypto API", version="0.0.1", lifespan=_lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-User-Id"],
)
app.include_router(api_router)
app.include_router(admin_router)
app.include_router(sso_router)

_DASHBOARD_DIR = Path(__file__).parent / "dashboard"


@app.get("/api/health")
def health() -> dict[str, str]:
    s = get_settings()
    return {"status": "ok", "env": s.app_env}


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """Friendly landing — send browsers to the dashboard."""
    return RedirectResponse(url="/dashboard")


def _has_valid_session(token: str | None) -> bool:
    if not token:
        return False
    try:
        verify_session_jwt(token)
    except HTTPException:
        return False
    return True


def _hub_launch_url() -> str | None:
    """Chalyb's cross-app launcher for this engine: it signs the visitor in (or
    sends them to /sign-in), checks the plan, mints the SSO token and comes back
    through /auth/sso."""
    hub = (os.environ.get("CHALYB_BASE_URL") or "").strip().rstrip("/")
    if not hub:
        return None
    slug = (os.environ.get("ENGINE_SLUG") or "chalybcrypto").strip()
    return f"{hub}/auth/launch/{slug}"


@app.get("/dashboard", include_in_schema=False)
def dashboard_index(
    nxc_session: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
) -> Response:
    """Serve the single-page dashboard. Per ARCHITECTURE, the production dashboard
    lives inside chalyb.com; this is the in-repo demo UI so the API is testable
    without spinning up Next.js.

    In jwt mode (always on Cloud Run) a visitor without a valid session is sent
    to Chalyb to sign in instead of getting a panel whose every call fails 401."""
    if auth_mode() == "jwt" and not _has_valid_session(nxc_session):
        launch = _hub_launch_url()
        if launch:
            return RedirectResponse(url=launch, status_code=302)
        return HTMLResponse(
            "<!DOCTYPE html><html lang=\"es\"><meta charset=\"utf-8\">"
            "<title>ChalyCrypto</title><p>Inicia sesión en Chalyb para abrir este panel.</p>",
            status_code=401,
        )
    return FileResponse(_DASHBOARD_DIR / "index.html")
