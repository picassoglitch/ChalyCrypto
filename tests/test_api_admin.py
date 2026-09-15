"""Admin endpoints — tenant provisioning + status (Chalyb integration backbone)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from chalybcrypto_api.deps import set_store_for_tests
from chalybcrypto_api.main import app
from chalybcrypto_api.store import InMemoryStore


ADMIN_TOKEN = "test-chalyb-admin-token"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("CHALYB_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.delenv("CHALYBCRYPTO_AUTH", raising=False)  # user routes stay in stub
    store = InMemoryStore()
    set_store_for_tests(store)
    with TestClient(app) as c:
        yield c, store


def _auth() -> dict:
    return {"Authorization": f"Bearer {ADMIN_TOKEN}"}


# ── /api/admin/tenants — provisioning ─────────────────────────────────────


def test_provision_returns_tenant_id_and_api_token(client):
    c, _ = client
    r = c.post(
        "/api/admin/tenants",
        headers=_auth(),
        json={
            "external_user_id": "44444444-4444-4444-4444-444444444444",
            "email": "u@example.com",
            "display_name": "Acme Trader",
            "tier": "pro",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert "tenant_id" in body
    assert "api_token" in body
    assert len(body["api_token"]) >= 20


def test_provision_is_idempotent_on_external_user_id(client):
    c, _ = client
    payload = {
        "external_user_id": "55555555-5555-5555-5555-555555555555",
        "email": "u@example.com",
        "tier": "pro",
    }
    a = c.post("/api/admin/tenants", headers=_auth(), json=payload).json()
    b = c.post("/api/admin/tenants", headers=_auth(), json=payload).json()
    assert a["tenant_id"] == b["tenant_id"]
    assert a["api_token"] == b["api_token"]


def test_provision_rejects_invalid_tier(client):
    c, _ = client
    r = c.post(
        "/api/admin/tenants",
        headers=_auth(),
        json={"external_user_id": "x", "email": "x@x", "tier": "platinum"},
    )
    assert r.status_code == 400


def test_provision_requires_admin_token(client):
    c, _ = client
    r = c.post(
        "/api/admin/tenants",
        json={"external_user_id": "x", "email": "x@x", "tier": "free"},
    )
    assert r.status_code == 401


def test_provision_rejects_wrong_admin_token(client):
    c, _ = client
    r = c.post(
        "/api/admin/tenants",
        headers={"Authorization": "Bearer wrong-token"},
        json={"external_user_id": "x", "email": "x@x", "tier": "free"},
    )
    assert r.status_code == 401


def test_admin_token_unset_returns_500(client, monkeypatch):
    monkeypatch.delenv("CHALYB_ADMIN_TOKEN", raising=False)
    c, _ = client
    r = c.post(
        "/api/admin/tenants",
        headers=_auth(),
        json={"external_user_id": "x", "email": "x@x", "tier": "free"},
    )
    assert r.status_code == 500
    assert "CHALYB_ADMIN_TOKEN" in r.json()["detail"]


# ── /api/admin/tenants/{id}/status — pause/resume ─────────────────────────


def test_set_status_active_then_paused(client):
    c, _ = client
    a = c.post(
        "/api/admin/tenants",
        headers=_auth(),
        json={"external_user_id": "u1", "email": "u@x", "tier": "pro"},
    ).json()
    tid = a["tenant_id"]

    r = c.post(
        f"/api/admin/tenants/{tid}/status",
        headers=_auth(),
        json={"status": "paused"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "paused"

    r = c.post(
        f"/api/admin/tenants/{tid}/status",
        headers=_auth(),
        json={"status": "active"},
    )
    assert r.json()["status"] == "active"


def test_set_status_unknown_tenant_returns_404(client):
    c, _ = client
    r = c.post(
        "/api/admin/tenants/00000000-0000-0000-0000-000000000099/status",
        headers=_auth(),
        json={"status": "paused"},
    )
    assert r.status_code == 404


def test_set_status_invalid_value_rejected(client):
    c, _ = client
    a = c.post(
        "/api/admin/tenants",
        headers=_auth(),
        json={"external_user_id": "u2", "email": "u@x", "tier": "free"},
    ).json()
    r = c.post(
        f"/api/admin/tenants/{a['tenant_id']}/status",
        headers=_auth(),
        json={"status": "wrecked"},
    )
    assert r.status_code == 400
