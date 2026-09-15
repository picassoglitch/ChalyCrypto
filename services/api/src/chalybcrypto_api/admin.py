"""Admin routes — Chalyb integration backbone.

These are called by the chalyb server (not by end-user browsers) to:

  - provision a tenant when a user clicks "Abrir ChalybCrypto" for the first time
  - pause / resume a tenant (e.g. when their subscription lapses)

Auth: Bearer token in `Authorization` header MUST match CHALYB_ADMIN_TOKEN.
This is a service-to-service token, NEVER exposed to the browser.
"""

from __future__ import annotations

import hmac
import os
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, EmailStr

from .deps import get_store
from .store import ApiStore


admin_router = APIRouter(prefix="/api/admin", tags=["admin"])


def _admin_token() -> str | None:
    return os.environ.get("CHALYB_ADMIN_TOKEN")


def require_admin(authorization: str | None = Header(default=None)) -> None:
    """Bearer-token gate. The expected value is CHALYB_ADMIN_TOKEN."""
    expected = _admin_token()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="CHALYB_ADMIN_TOKEN not configured on this server",
        )
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing Bearer token")
    presented = authorization[7:].strip()
    # constant-time compare to avoid timing leaks
    if not hmac.compare_digest(presented, expected):
        raise HTTPException(status_code=401, detail="invalid admin token")


class ProvisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    external_user_id: str
    email: str
    display_name: str | None = None
    tier: str = "free"


_VALID_TIERS = {"free", "pro", "all_access"}


@admin_router.post("/tenants")
async def provision_tenant(
    body: ProvisionRequest,
    _: None = Depends(require_admin),
    store: ApiStore = Depends(get_store),
) -> dict:
    """Create or return an existing tenant. Idempotent on external_user_id.
    Returns 200 (created or existing) with {tenant_id, api_token}."""
    if body.tier not in _VALID_TIERS:
        raise HTTPException(
            status_code=400,
            detail=f"tier must be one of {sorted(_VALID_TIERS)}",
        )
    tenant, _created = await store.provision_tenant(
        external_user_id=body.external_user_id,
        email=body.email,
        display_name=body.display_name,
        tier=body.tier,
    )
    # chalyb expects {tenant_id, api_token} regardless of whether it's fresh
    # or a re-grant. The integration treats 200/201/409 all the same.
    return {
        "tenant_id": str(tenant["id"]),
        "api_token": tenant["api_token"],
    }


class StatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str


_VALID_STATUSES = {"active", "paused"}


@admin_router.post("/tenants/{tenant_id}/status")
async def set_tenant_status(
    tenant_id: UUID,
    body: StatusRequest,
    _: None = Depends(require_admin),
    store: ApiStore = Depends(get_store),
) -> dict:
    if body.status not in _VALID_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of {sorted(_VALID_STATUSES)}",
        )
    row = await store.set_tenant_status(tenant_id=tenant_id, status=body.status)
    if row is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return {"tenant_id": str(row["id"]), "status": row["status"]}
