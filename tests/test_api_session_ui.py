"""Dashboard + /api/me behaviour with the Chalyb SSO session (jwt mode)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from chalybcrypto_api.consumption import set_hub_client_for_tests
from chalybcrypto_api.deps import set_store_for_tests
from chalybcrypto_api.main import app
from chalybcrypto_api.sso import SESSION_COOKIE_NAME, mint_session_jwt
from chalybcrypto_api.store import InMemoryStore


_SSO_SECRET = "test-shared-sso-secret-must-be-long-enough-32b"
USER_ID = str(uuid4())


@pytest.fixture
def jwt_client(monkeypatch):
    monkeypatch.setenv("CHALYB_SSO_SECRET", _SSO_SECRET)
    monkeypatch.setenv("CHALYBCRYPTO_AUTH", "jwt")
    monkeypatch.setenv("CHALYB_BASE_URL", "https://www.chalyb.com/")
    monkeypatch.setenv("ENGINE_SLUG", "chalybcrypto")
    set_store_for_tests(InMemoryStore())
    # CHALYB_BASE_URL would otherwise build and cache a real hub client that
    # outlives this test (and its env) and breaks admission in later tests.
    set_hub_client_for_tests(None)
    with TestClient(app) as c:
        yield c
    set_hub_client_for_tests(None)


def _session() -> str:
    return mint_session_jwt(user_id=USER_ID, email="dueno@example.com")


def test_signed_out_dashboard_goes_to_the_hub_launcher(jwt_client):
    r = jwt_client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "https://www.chalyb.com/auth/launch/chalybcrypto"


def test_launcher_uses_the_documented_engine_slug(jwt_client, monkeypatch):
    monkeypatch.setenv("CHALYB_ENGINE_SLUG", "senales-beta")
    r = jwt_client.get("/dashboard", follow_redirects=False)
    assert r.headers["location"] == "https://www.chalyb.com/auth/launch/senales-beta"


def test_forged_session_cookie_is_treated_as_signed_out(jwt_client):
    forged = mint_session_jwt(user_id=USER_ID, email="x@example.com")
    jwt_client.cookies.set(SESSION_COOKIE_NAME, forged[:-4] + "AAAA")
    r = jwt_client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 302


def test_signed_out_dashboard_without_hub_url_is_401(jwt_client, monkeypatch):
    monkeypatch.delenv("CHALYB_BASE_URL")
    r = jwt_client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 401
    assert "Inicia sesión" in r.text


def test_signed_in_dashboard_is_served(jwt_client):
    jwt_client.cookies.set(SESSION_COOKIE_NAME, _session())
    r = jwt_client.get("/dashboard", follow_redirects=False)
    assert r.status_code == 200
    assert "user-label" in r.text


def test_me_returns_the_session_email(jwt_client):
    jwt_client.cookies.set(SESSION_COOKIE_NAME, _session())
    r = jwt_client.get("/api/me")
    assert r.status_code == 200
    assert r.json() == {"user_id": USER_ID, "email": "dueno@example.com", "auth": "jwt"}


def test_me_signed_out_is_401(jwt_client):
    assert jwt_client.get("/api/me").status_code == 401


def test_me_ignores_x_user_id_in_jwt_mode(jwt_client):
    r = jwt_client.get("/api/me", headers={"X-User-Id": str(uuid4())})
    assert r.status_code == 401


def test_me_in_stub_mode_says_stub(monkeypatch):
    monkeypatch.setenv("CHALYBCRYPTO_AUTH", "stub")
    set_store_for_tests(InMemoryStore())
    uid = str(uuid4())
    with TestClient(app) as c:
        r = c.get("/api/me", headers={"X-User-Id": uid})
    assert r.status_code == 200
    assert r.json() == {"user_id": uid, "email": None, "auth": "stub"}


def test_health_reports_production_on_cloud_run(monkeypatch):
    from chalybcrypto_shared.config import get_settings

    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("K_SERVICE", "chalybcrypto")
    get_settings.cache_clear()
    try:
        with TestClient(app) as c:
            r = c.get("/api/health")
        assert r.json()["env"] == "production"
    finally:
        get_settings.cache_clear()
