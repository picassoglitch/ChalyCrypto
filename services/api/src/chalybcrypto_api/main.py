from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse

from chalybcrypto_shared import get_settings

from .admin import admin_router
from .routes import router as api_router
from .sso import sso_router


def _cors_origins() -> list[str]:
    """Pulled from CHALYBCRYPTO_CORS_ORIGINS (csv). Default = localhost-only so dev
    works out of the box but production has to opt in explicitly."""
    raw = os.environ.get(
        "CHALYBCRYPTO_CORS_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000",
    )
    return [o.strip() for o in raw.split(",") if o.strip()]


app = FastAPI(title="ChalybCrypto API", version="0.0.1")
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


@app.get("/dashboard", include_in_schema=False)
def dashboard_index() -> FileResponse:
    """Serve the single-page dashboard. Per ARCHITECTURE, the production dashboard
    lives inside chalyb.com; this is the in-repo demo UI so the API is testable
    without spinning up Next.js."""
    return FileResponse(_DASHBOARD_DIR / "index.html")
