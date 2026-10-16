"""Hub API auth — the Chalyb hub's server calls this engine on a user's behalf.

The hub (Next.js BFF, never the browser) mints a short-lived token per request
in the same format as the SSO launch token (sso.py):

    <body>.<sig>
    body = base64url(JSON {user_id, email, tenant_id, tier, exp, aud})
    sig  = base64url(HMAC-SHA256(body, CHALYB_SSO_SECRET))

and sends it as `Authorization: Bearer <body>.<sig>` to /api/hub/v1/*.

Differences from the SSO launch token, so one can never stand in for the other:
  * `aud` MUST be "chalybcrypto-hub-api". A launch token (no aud) travels in a
    URL and can end up in logs; it is refused here. /auth/sso refuses this aud.
  * `exp` must be in the future and at most HUB_TOKEN_MAX_TTL_SECONDS away, so
    a leaked token is short-lived even if the hub mis-set its lifetime.
  * `user_id` must be a UUID: it is the hub user id, the same id the SSO flow
    uses as the session `sub`, so a person sees the same data either way.

No cookie, no X-User-Id, no Supabase JWT is accepted on /api/hub/v1, whatever
CHALYBCRYPTO_AUTH says.
"""

from __future__ import annotations

import base64
import hmac
import json
import time
from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID

from fastapi import Header, HTTPException, Request

from .sso import _sso_secret

HUB_TOKEN_AUDIENCE = "chalybcrypto-hub-api"
HUB_TOKEN_MAX_TTL_SECONDS = 600


@dataclass(frozen=True)
class HubPrincipal:
    user_id: UUID
    email: str
    tier: str
    tenant_id: str


def _b64url_sig(secret: str, body: str) -> str:
    return (
        base64.urlsafe_b64encode(hmac.new(secret.encode("utf-8"), body.encode("utf-8"), sha256).digest())
        .rstrip(b"=")
        .decode("ascii")
    )


def verify_hub_token(token: str, *, now: float | None = None) -> HubPrincipal:
    """Verify signature, audience, lifetime and subject. 401 on any failure,
    with one generic detail so a caller can't probe which check failed."""
    bad = HTTPException(status_code=401, detail="invalid hub token")
    if token.count(".") != 1:
        raise bad
    body, sig = token.split(".", 1)
    if not body or not sig:
        raise bad
    secret = _sso_secret()  # 500 when the engine isn't configured
    if not hmac.compare_digest(_b64url_sig(secret, body), sig):
        raise bad
    try:
        pad = "=" * (-len(body) % 4)
        claims = json.loads(base64.urlsafe_b64decode(body + pad).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise bad from None
    if not isinstance(claims, dict) or claims.get("aud") != HUB_TOKEN_AUDIENCE:
        raise bad
    t = time.time() if now is None else now
    exp = claims.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        raise bad
    if exp <= t:
        raise HTTPException(status_code=401, detail="hub token expired")
    if exp > t + HUB_TOKEN_MAX_TTL_SECONDS:
        raise bad
    try:
        user_id = UUID(str(claims.get("user_id")))
    except (TypeError, ValueError):
        raise bad from None
    email = claims.get("email")
    if not isinstance(email, str) or not email:
        raise bad
    tier = claims.get("tier")
    tenant_id = claims.get("tenant_id")
    return HubPrincipal(
        user_id=user_id,
        email=email,
        tier=tier if isinstance(tier, str) else "",
        tenant_id=tenant_id if isinstance(tenant_id, str) else "",
    )


async def hub_principal(
    request: Request,
    authorization: str | None = Header(default=None),
) -> HubPrincipal:
    if authorization is None or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing hub Bearer token")
    principal = verify_hub_token(authorization[7:].strip())
    request.state.hub_principal = principal
    return principal


async def hub_user_id(
    request: Request,
    authorization: str | None = Header(default=None),
) -> UUID:
    """Drop-in for auth.current_user_id on the /api/hub/v1 app."""
    return (await hub_principal(request, authorization)).user_id
