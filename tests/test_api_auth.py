"""API auth — JWT mode + X-User-Id stub mode."""

from __future__ import annotations

import os
import time
from uuid import uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from chalybcrypto_api.main import app


_TEST_SECRET = "test-jwt-secret-do-not-use-in-prod"
USER = uuid4()


@pytest.fixture
def stub_client(monkeypatch):
    monkeypatch.setenv("CHALYBCRYPTO_AUTH", "stub")
    with TestClient(app) as c:
        yield c


@pytest.fixture
def jwt_client(monkeypatch):
    monkeypatch.setenv("CHALYBCRYPTO_AUTH", "jwt")
    monkeypatch.setenv("CHALYBCRYPTO_SUPABASE_JWT_SECRET", _TEST_SECRET)
    with TestClient(app) as c:
        yield c


def _sign(claims: dict, *, secret: str = _TEST_SECRET) -> str:
    return jwt.encode(claims, secret, algorithm="HS256")


# ── stub mode (default; existing curl flows work) ──────────────────────────


def test_stub_mode_accepts_x_user_id(stub_client):
    r = stub_client.get("/api/signals", headers={"X-User-Id": str(USER)})
    assert r.status_code == 200


def test_stub_mode_rejects_missing_header(stub_client):
    r = stub_client.get("/api/signals")
    assert r.status_code == 401
    assert "X-User-Id" in r.json()["detail"]


def test_stub_mode_rejects_bad_uuid(stub_client):
    r = stub_client.get("/api/signals", headers={"X-User-Id": "not-a-uuid"})
    assert r.status_code == 400


# ── jwt mode (production) ──────────────────────────────────────────────────


def test_jwt_mode_accepts_valid_supabase_token(jwt_client):
    token = _sign(
        {
            "sub": str(USER),
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
        }
    )
    r = jwt_client.get("/api/signals", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_jwt_mode_rejects_missing_authorization(jwt_client):
    r = jwt_client.get("/api/signals")
    assert r.status_code == 401
    assert "Bearer" in r.json()["detail"]


def test_jwt_mode_rejects_wrong_scheme(jwt_client):
    r = jwt_client.get("/api/signals", headers={"Authorization": "Basic deadbeef"})
    assert r.status_code == 401


def test_jwt_mode_rejects_expired_token(jwt_client):
    token = _sign(
        {
            "sub": str(USER),
            "aud": "authenticated",
            "exp": int(time.time()) - 60,  # 1 minute ago
            "iat": int(time.time()) - 120,
        }
    )
    r = jwt_client.get("/api/signals", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert "expired" in r.json()["detail"]


def test_jwt_mode_rejects_wrong_signature(jwt_client):
    token = _sign(
        {
            "sub": str(USER),
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
        },
        secret="some-other-secret",
    )
    r = jwt_client.get("/api/signals", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_jwt_mode_rejects_wrong_audience(jwt_client):
    token = _sign(
        {
            "sub": str(USER),
            "aud": "service",  # not 'authenticated'
            "exp": int(time.time()) + 3600,
        }
    )
    r = jwt_client.get("/api/signals", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert "audience" in r.json()["detail"]


def test_jwt_mode_rejects_missing_sub(jwt_client):
    token = _sign(
        {
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
        }
    )
    r = jwt_client.get("/api/signals", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401


def test_jwt_mode_rejects_non_uuid_sub(jwt_client):
    token = _sign(
        {
            "sub": "not-a-uuid",
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
        }
    )
    r = jwt_client.get("/api/signals", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert "uuid" in r.json()["detail"]


def test_jwt_mode_with_missing_server_secret_returns_500(jwt_client, monkeypatch):
    """If the operator misconfigures the deploy, fail loudly."""
    monkeypatch.delenv("CHALYBCRYPTO_SUPABASE_JWT_SECRET", raising=False)
    monkeypatch.delenv("CHALYBCRYPTO_SUPABASE_URL", raising=False)
    token = _sign(
        {
            "sub": str(USER),
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
        }
    )
    r = jwt_client.get("/api/signals", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 500
    # Detail names BOTH env vars now (either is acceptable, neither = misconfig).
    assert "CHALYBCRYPTO_SUPABASE_URL" in r.json()["detail"]
    assert "CHALYBCRYPTO_SUPABASE_JWT_SECRET" in r.json()["detail"]


# ── new: JWKS path (preferred for current Supabase projects) ────────────


@pytest.fixture
def jwks_client_and_keys(monkeypatch):
    """Set up a fresh RSA keypair, monkeypatch the JWKS client to serve the
    public key, and configure the env vars for the JWKS verification path."""
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.backends import default_backend

    private_key = rsa.generate_private_key(
        public_exponent=65537, key_size=2048, backend=default_backend()
    )
    public_key = private_key.public_key()

    # Mock PyJWKClient.get_signing_key_from_jwt to return our public key.
    class _FakeSigningKey:
        def __init__(self, key):
            self.key = key

    class _FakeJWKSClient:
        def __init__(self, *a, **kw):
            pass
        def get_signing_key_from_jwt(self, token):
            return _FakeSigningKey(public_key)

    monkeypatch.setattr("chalybcrypto_api.auth.PyJWKClient", _FakeJWKSClient)
    # Also bust the lru_cache so a previous test's client doesn't stick.
    import chalybcrypto_api.auth as auth_mod
    auth_mod._jwks_client_for.cache_clear()  # type: ignore[attr-defined]

    monkeypatch.setenv("CHALYBCRYPTO_AUTH", "jwt")
    monkeypatch.setenv("CHALYBCRYPTO_SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.delenv("CHALYBCRYPTO_SUPABASE_JWT_SECRET", raising=False)

    with TestClient(app) as c:
        yield c, private_key


def _sign_rs256(claims: dict, private_key) -> str:
    return jwt.encode(claims, private_key, algorithm="RS256")


def test_jwks_path_accepts_valid_rs256_token(jwks_client_and_keys):
    c, priv = jwks_client_and_keys
    token = _sign_rs256(
        {
            "sub": str(USER),
            "aud": "authenticated",
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
        },
        priv,
    )
    r = c.get("/api/signals", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_jwks_path_rejects_expired_token(jwks_client_and_keys):
    c, priv = jwks_client_and_keys
    token = _sign_rs256(
        {
            "sub": str(USER),
            "aud": "authenticated",
            "exp": int(time.time()) - 60,
            "iat": int(time.time()) - 120,
        },
        priv,
    )
    r = c.get("/api/signals", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert "expired" in r.json()["detail"]


def test_jwks_path_rejects_wrong_audience(jwks_client_and_keys):
    c, priv = jwks_client_and_keys
    token = _sign_rs256(
        {
            "sub": str(USER),
            "aud": "service",
            "exp": int(time.time()) + 3600,
        },
        priv,
    )
    r = c.get("/api/signals", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
