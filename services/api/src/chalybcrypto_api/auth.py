"""Supabase JWT verification.

Two verification paths, picked automatically based on env config:

  NEW (preferred — Supabase JWT Signing Keys):
    Set CHALYBCRYPTO_SUPABASE_URL=https://<ref>.supabase.co. We fetch the JWKS
    from <url>/auth/v1/.well-known/jwks.json and verify with the published
    public key. Algorithms: RS256, ES256 (whatever Supabase signed with).
    PyJWKClient caches keys in-process so it's a single fetch per cold start.

  LEGACY (compat — HS256 shared secret):
    Set CHALYBCRYPTO_SUPABASE_JWT_SECRET to the legacy JWT secret. Still supported
    by Supabase for backward compat but you should migrate to JWKS — Supabase
    deprecated the legacy secret in late 2025.

Two modes for the dependency itself:
  - `jwt`  : verify a Bearer token (production)
  - `stub` : trust an `X-User-Id` header (local dev, tests, the dashboard demo)

Selected by `CHALYBCRYPTO_AUTH` env var; default `stub`.

CLAUDE.md rule 7: secrets never leave the server and never appear in logs.
"""

from __future__ import annotations

import os
from functools import lru_cache
from uuid import UUID

import jwt
from fastapi import Cookie, Header, HTTPException, status
from jwt import PyJWKClient


_DEFAULT_MODE = "stub"
_JWT_AUDIENCE = "authenticated"  # Supabase default audience for user tokens
_JWKS_ALGORITHMS = ["RS256", "ES256"]


def _mode() -> str:
    return (os.environ.get("CHALYBCRYPTO_AUTH") or _DEFAULT_MODE).strip().lower()


@lru_cache(maxsize=1)
def _jwks_client_for(url: str) -> PyJWKClient:
    """Cached per-URL JWKS client. PyJWKClient caches the JWKS internally too;
    this LRU is just to avoid re-instantiation per request."""
    return PyJWKClient(url, cache_keys=True, lifespan=3600)


def _verify_with_jwks(token: str, supabase_url: str) -> dict:
    jwks_url = f"{supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
    try:
        signing_key = _jwks_client_for(jwks_url).get_signing_key_from_jwt(token).key
    except jwt.PyJWKClientError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"could not fetch Supabase JWKS: {e}",
        ) from e
    return jwt.decode(
        token,
        signing_key,
        algorithms=_JWKS_ALGORITHMS,
        audience=_JWT_AUDIENCE,
        options={"require": ["sub", "exp"]},
    )


def _verify_with_legacy_secret(token: str, secret: str) -> dict:
    return jwt.decode(
        token,
        secret,
        algorithms=["HS256"],
        audience=_JWT_AUDIENCE,
        options={"require": ["sub", "exp"]},
    )


def _verify_bearer(token: str) -> UUID:
    """Verify a Supabase JWT. Raise HTTPException 401 on any failure.
    Prefers JWKS (modern); falls back to legacy HS256 secret if URL is unset."""
    supabase_url = os.environ.get("CHALYBCRYPTO_SUPABASE_URL")
    legacy_secret = os.environ.get("CHALYBCRYPTO_SUPABASE_JWT_SECRET")

    if not supabase_url and not legacy_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "CHALYBCRYPTO_AUTH=jwt requires CHALYBCRYPTO_SUPABASE_URL "
                "(JWKS, preferred) or CHALYBCRYPTO_SUPABASE_JWT_SECRET (legacy compat)"
            ),
        )

    try:
        if supabase_url:
            claims = _verify_with_jwks(token, supabase_url)
        else:
            claims = _verify_with_legacy_secret(token, legacy_secret)  # type: ignore[arg-type]
    except jwt.ExpiredSignatureError as e:
        raise HTTPException(status_code=401, detail="token expired") from e
    except jwt.InvalidAudienceError as e:
        raise HTTPException(status_code=401, detail="wrong audience") from e
    except jwt.InvalidTokenError as e:
        # Bucket every other JWT error under one detail so we don't leak which check failed.
        raise HTTPException(status_code=401, detail="invalid token") from e
    sub = claims.get("sub")
    if not sub:
        raise HTTPException(status_code=401, detail="missing sub")
    try:
        return UUID(str(sub))
    except ValueError as e:
        raise HTTPException(status_code=401, detail="sub is not a uuid") from e


async def current_user_id(
    authorization: str | None = Header(default=None),
    x_user_id: str | None = Header(default=None),
    nxc_session: str | None = Cookie(default=None),
) -> UUID:
    """FastAPI dependency. Resolves user_id from JWT, session cookie, or stub.

    In `jwt` mode (production):
      1. Try nxc_session cookie (minted by /auth/sso). Single hop from a Chalyb
         "Abrir ChalyCrypto" click — best UX, no Bearer juggling on every
         dashboard request.
      2. Fall back to Authorization: Bearer for direct-API callers (CLI, tests).
    In `stub` mode (local dev): X-User-Id header.
    """
    mode = _mode()
    if mode == "jwt":
        if nxc_session:
            # Local import keeps the route layer's import graph small.
            from .sso import verify_session_jwt

            claims = verify_session_jwt(nxc_session)
            sub = claims.get("sub")
            try:
                return UUID(str(sub))
            except (TypeError, ValueError) as e:
                raise HTTPException(status_code=401, detail="session sub is not a uuid") from e
        if authorization is None or not authorization.lower().startswith("bearer "):
            raise HTTPException(
                status_code=401,
                detail="missing Bearer token or session cookie",
            )
        return _verify_bearer(authorization[7:].strip())
    # stub mode: keep existing X-User-Id behaviour
    if x_user_id is None:
        raise HTTPException(
            status_code=401,
            detail="missing X-User-Id (auth stub; set CHALYBCRYPTO_AUTH=jwt in prod)",
        )
    try:
        return UUID(x_user_id)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"X-User-Id must be a uuid: {e}",
        ) from e
