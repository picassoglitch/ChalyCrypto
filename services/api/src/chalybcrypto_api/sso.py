"""SSO landing — verifies the launch token from Chalyb and mints a session cookie.

Flow:
  Browser hits GET /auth/sso?token=<body>.<sig>
    body = base64url(JSON of {user_id, email, tenant_id, tier, exp})
    sig  = HMAC-SHA256 of body with CHALYB_SSO_SECRET
  This handler:
    1. Verifies sig with CHALYB_SSO_SECRET (constant-time compare).
    2. Checks exp.
    3. Mints a session JWT (HS256, signed with CHALYB_SSO_SECRET) with
       sub = user_id, aud = 'chalybcrypto-session', exp = 24h out.
    4. Sets it as an httponly Secure SameSite=Lax cookie named 'nxc_session'.
    5. 302-redirects to /dashboard.

The auth dependency (auth.py) accepts this cookie as an alternate to a
Supabase JWT Bearer token in the same `jwt` mode.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import time
from hashlib import sha256

import jwt
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse


sso_router = APIRouter(tags=["sso"])


SESSION_COOKIE_NAME = "nxc_session"
SESSION_TTL_SECONDS = 60 * 60 * 24  # 24h


def _sso_secret() -> str:
    secret = os.environ.get("CHALYB_SSO_SECRET")
    if not secret:
        raise HTTPException(status_code=500, detail="CHALYB_SSO_SECRET not configured")
    return secret


def _decode_launch_token(token: str) -> dict:
    """Verify the HMAC and exp on a token chalyb signed for us. Returns the
    payload claims. Raises 401 on any failure."""
    if "." not in token:
        raise HTTPException(status_code=401, detail="malformed launch token")
    body, sig = token.rsplit(".", 1)
    secret = _sso_secret()
    expected_sig = (
        base64.urlsafe_b64encode(
            hmac.new(secret.encode("utf-8"), body.encode("utf-8"), sha256).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    if not hmac.compare_digest(expected_sig, sig):
        raise HTTPException(status_code=401, detail="bad launch token signature")
    try:
        pad = "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(body + pad).decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=401, detail="launch token payload not decodable") from e
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or exp < time.time():
        raise HTTPException(status_code=401, detail="launch token expired")
    for required in ("user_id", "email"):
        if required not in payload:
            raise HTTPException(status_code=401, detail=f"launch token missing {required}")
    return payload


def mint_session_jwt(*, user_id: str, email: str, ttl_seconds: int = SESSION_TTL_SECONDS) -> str:
    """Sign a session JWT with CHALYB_SSO_SECRET. Same secret chalyb uses to
    sign launch tokens — keeps the env-var set small. aud='chalybcrypto-session'
    so it can't be confused for a launch token or a Supabase JWT."""
    secret = _sso_secret()
    return jwt.encode(
        {
            "sub": user_id,
            "email": email,
            "aud": "chalybcrypto-session",
            "exp": int(time.time()) + ttl_seconds,
            "iat": int(time.time()),
        },
        secret,
        algorithm="HS256",
    )


def verify_session_jwt(token: str) -> dict:
    """Mirror of mint — raises HTTPException 401 on any verification failure."""
    secret = _sso_secret()
    try:
        return jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            audience="chalybcrypto-session",
            options={"require": ["sub", "exp"]},
        )
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(status_code=401, detail="session expired") from e
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail="invalid session") from e


@sso_router.get("/auth/sso", include_in_schema=False)
async def sso_landing(token: str = Query(...)) -> RedirectResponse:
    payload = _decode_launch_token(token)
    session = mint_session_jwt(user_id=payload["user_id"], email=payload["email"])
    response = RedirectResponse(url="/dashboard", status_code=302)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session,
        httponly=True,
        secure=os.environ.get("CHALYBCRYPTO_ENV", "prod").lower() != "dev",
        samesite="lax",
        max_age=SESSION_TTL_SECONDS,
        path="/",
    )
    return response


@sso_router.post("/auth/logout", include_in_schema=False)
async def logout():
    """Clear the session cookie."""
    from fastapi.responses import JSONResponse

    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return resp
