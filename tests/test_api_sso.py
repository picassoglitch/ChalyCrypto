"""SSO landing — verifies launch token, mints session cookie, redirects."""

from __future__ import annotations

import base64
import hmac
import json
import time
from hashlib import sha256
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from chalybcrypto_api.main import app


_SSO_SECRET = "test-shared-sso-secret-must-be-long-enough-32b"
USER_ID = str(uuid4())


def _sign_launch_token(payload: dict, *, secret: str = _SSO_SECRET) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).rstrip(b"=").decode("ascii")
    sig = base64.urlsafe_b64encode(
        hmac.new(secret.encode("utf-8"), body.encode("utf-8"), sha256).digest()
    ).rstrip(b"=").decode("ascii")
    return f"{body}.{sig}"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("CHALYB_SSO_SECRET", _SSO_SECRET)
    monkeypatch.setenv("CHALYBCRYPTO_AUTH", "jwt")  # so the session cookie is honored
    monkeypatch.setenv("CHALYBCRYPTO_ENV", "dev")    # cookie not Secure, easier to assert
    with TestClient(app) as c:
        yield c


# ── happy path ─────────────────────────────────────────────────────────────


def test_sso_with_valid_token_redirects_to_dashboard_and_sets_cookie(client):
    token = _sign_launch_token(
        {
            "user_id": USER_ID,
            "email": "u@example.com",
            "tenant_id": str(uuid4()),
            "tier": "pro",
            "exp": int(time.time()) + 60,
        }
    )
    r = client.get(f"/auth/sso?token={token}", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/dashboard"
    # Cookie set
    set_cookie = r.headers.get("set-cookie", "")
    assert "nxc_session=" in set_cookie
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()


def test_session_cookie_authenticates_subsequent_api_call(client):
    # 1. Land via SSO
    token = _sign_launch_token(
        {
            "user_id": USER_ID,
            "email": "u@example.com",
            "tenant_id": str(uuid4()),
            "tier": "pro",
            "exp": int(time.time()) + 60,
        }
    )
    client.get(f"/auth/sso?token={token}", follow_redirects=False)
    # 2. The cookie is now on the client; hit a protected route with no header
    r = client.get("/api/signals")
    assert r.status_code == 200, r.text


# ── verification failures ─────────────────────────────────────────────────


def test_sso_rejects_bad_signature(client):
    token = _sign_launch_token(
        {"user_id": USER_ID, "email": "u@x", "exp": int(time.time()) + 60},
        secret="wrong-secret-on-the-other-side",
    )
    r = client.get(f"/auth/sso?token={token}", follow_redirects=False)
    assert r.status_code == 401
    assert "signature" in r.json()["detail"]


def test_sso_rejects_expired_token(client):
    token = _sign_launch_token(
        {"user_id": USER_ID, "email": "u@x", "exp": int(time.time()) - 60}
    )
    r = client.get(f"/auth/sso?token={token}", follow_redirects=False)
    assert r.status_code == 401
    assert "expired" in r.json()["detail"]


def test_sso_rejects_malformed_token(client):
    r = client.get("/auth/sso?token=not-a-real-token", follow_redirects=False)
    assert r.status_code == 401


def test_sso_rejects_missing_user_id(client):
    token = _sign_launch_token({"email": "u@x", "exp": int(time.time()) + 60})
    r = client.get(f"/auth/sso?token={token}", follow_redirects=False)
    assert r.status_code == 401
    assert "user_id" in r.json()["detail"]


def test_sso_with_missing_server_secret_returns_500(client, monkeypatch):
    monkeypatch.delenv("CHALYB_SSO_SECRET", raising=False)
    token = _sign_launch_token(
        {"user_id": USER_ID, "email": "u@x", "exp": int(time.time()) + 60}
    )
    r = client.get(f"/auth/sso?token={token}", follow_redirects=False)
    assert r.status_code == 500


# ── logout clears the cookie ──────────────────────────────────────────────


def test_logout_clears_session_cookie(client):
    token = _sign_launch_token(
        {"user_id": USER_ID, "email": "u@x", "exp": int(time.time()) + 60}
    )
    client.get(f"/auth/sso?token={token}", follow_redirects=False)
    r = client.post("/auth/logout")
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie", "")
    # delete_cookie sets max-age=0 (or expires in the past) for nxc_session
    assert "nxc_session=" in set_cookie
    assert "max-age=0" in set_cookie.lower() or "1970" in set_cookie.lower()


# ── auth dependency: cookie takes precedence over missing Bearer ─────────


def test_invalid_session_cookie_rejected(client):
    client.cookies.set("nxc_session", "garbage.not.a.real.jwt")
    r = client.get("/api/signals")
    assert r.status_code == 401
