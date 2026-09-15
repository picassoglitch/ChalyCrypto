"""Exchange connection routes — encrypted at rest, secrets never returned."""

from __future__ import annotations

from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from chalybcrypto_api.deps import set_store_for_tests
from chalybcrypto_api.main import app
from chalybcrypto_api.store import InMemoryStore


@pytest.fixture
def client(monkeypatch):
    # Fresh in-memory store + a real Fernet key so vault.encrypt actually runs.
    monkeypatch.setenv("CHALYBCRYPTO_MASTER_ENCRYPTION_KEY", Fernet.generate_key().decode())
    store = InMemoryStore()
    set_store_for_tests(store)
    with TestClient(app) as c:
        yield c, store


def _auth(user: str = "11111111-1111-1111-1111-111111111111") -> dict:
    return {"X-User-Id": user}


# ── happy path ─────────────────────────────────────────────────────────────


def test_post_connection_returns_no_secrets(client):
    c, store = client
    r = c.post(
        "/api/connections/exchange",
        headers=_auth(),
        json={
            "exchange": "bitunix",
            "api_key": "ak_super_secret_key_42",
            "api_secret": "sk_even_more_secret_99",
        },
    )
    assert r.status_code == 201
    body = r.json()
    # Response NEVER includes plaintext OR ciphertext (CLAUDE.md rule 7).
    flat = str(body).lower()
    assert "ak_super_secret" not in flat
    assert "sk_even_more_secret" not in flat
    assert "api_key" not in body
    assert "api_secret" not in body
    assert "api_key_enc" not in body
    assert "api_secret_enc" not in body
    # But it does carry the identifying fields.
    assert body["exchange"] == "bitunix"
    assert body["status"] == "untested"


def test_post_then_list_round_trip(client):
    c, _ = client
    c.post(
        "/api/connections/exchange",
        headers=_auth(),
        json={"exchange": "bitunix", "api_key": "k1", "api_secret": "s1"},
    )
    c.post(
        "/api/connections/exchange",
        headers=_auth(),
        json={"exchange": "lbank", "api_key": "k2", "api_secret": "s2"},
    )

    r = c.get("/api/connections/exchange", headers=_auth())
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    exchanges = {row["exchange"] for row in rows}
    assert exchanges == {"bitunix", "lbank"}
    # No row should contain anything secret-looking.
    flat = str(rows).lower()
    assert "k1" not in flat and "k2" not in flat
    assert "s1" not in flat and "s2" not in flat


def test_in_memory_store_actually_stores_ciphertext_not_plaintext(client):
    c, store = client
    c.post(
        "/api/connections/exchange",
        headers=_auth(),
        json={
            "exchange": "bitunix",
            "api_key": "PLAINTEXT_KEY_LEAK_CANARY",
            "api_secret": "PLAINTEXT_SECRET_LEAK_CANARY",
        },
    )
    # Reach into the store and verify the raw blob is encrypted Fernet token.
    record = store._connections[0]  # type: ignore[attr-defined]
    assert b"PLAINTEXT_KEY_LEAK_CANARY" not in record["api_key_enc"]
    assert b"PLAINTEXT_SECRET_LEAK_CANARY" not in record["api_secret_enc"]
    # Fernet ciphertext starts with version byte 0x80 then base64url chars.
    assert record["api_key_enc"].startswith(b"gAAAAA")
    assert record["api_secret_enc"].startswith(b"gAAAAA")


def test_ip_allowlist_persists(client):
    c, store = client
    c.post(
        "/api/connections/exchange",
        headers=_auth(),
        json={
            "exchange": "bitunix",
            "api_key": "k",
            "api_secret": "s",
            "ip_allowlist": ["203.0.113.1", "203.0.113.2"],
        },
    )
    r = c.get("/api/connections/exchange", headers=_auth())
    assert r.json()[0]["ip_allowlist"] == ["203.0.113.1", "203.0.113.2"]


# ── validation ─────────────────────────────────────────────────────────────


def test_post_rejects_unknown_exchange(client):
    c, _ = client
    r = c.post(
        "/api/connections/exchange",
        headers=_auth(),
        json={"exchange": "kraken", "api_key": "k", "api_secret": "s"},
    )
    assert r.status_code == 400
    assert "unknown exchange" in r.json()["detail"]


def test_post_rejects_empty_key_or_secret(client):
    c, _ = client
    r = c.post(
        "/api/connections/exchange",
        headers=_auth(),
        json={"exchange": "bitunix", "api_key": "", "api_secret": "s"},
    )
    assert r.status_code == 400


def test_post_rejects_extra_fields_per_pydantic_strict(client):
    c, _ = client
    r = c.post(
        "/api/connections/exchange",
        headers=_auth(),
        json={"exchange": "bitunix", "api_key": "k", "api_secret": "s", "evil": "x"},
    )
    assert r.status_code == 422


def test_missing_master_key_returns_500_with_clear_message(client, monkeypatch):
    monkeypatch.delenv("CHALYBCRYPTO_MASTER_ENCRYPTION_KEY", raising=False)
    c, _ = client
    r = c.post(
        "/api/connections/exchange",
        headers=_auth(),
        json={"exchange": "bitunix", "api_key": "k", "api_secret": "s"},
    )
    assert r.status_code == 500
    assert "CHALYBCRYPTO_MASTER_ENCRYPTION_KEY" in r.json()["detail"]


# ── per-user isolation ────────────────────────────────────────────────────


def test_connections_are_per_user(client):
    c, _ = client
    user_a = "22222222-2222-2222-2222-222222222222"
    user_b = "33333333-3333-3333-3333-333333333333"
    c.post(
        "/api/connections/exchange",
        headers={"X-User-Id": user_a},
        json={"exchange": "bitunix", "api_key": "ka", "api_secret": "sa"},
    )
    c.post(
        "/api/connections/exchange",
        headers={"X-User-Id": user_b},
        json={"exchange": "lbank", "api_key": "kb", "api_secret": "sb"},
    )

    r_a = c.get("/api/connections/exchange", headers={"X-User-Id": user_a})
    r_b = c.get("/api/connections/exchange", headers={"X-User-Id": user_b})
    assert len(r_a.json()) == 1
    assert len(r_b.json()) == 1
    assert r_a.json()[0]["exchange"] == "bitunix"
    assert r_b.json()[0]["exchange"] == "lbank"


def test_unauthed_request_blocked(client):
    c, _ = client
    r = c.post(
        "/api/connections/exchange",
        json={"exchange": "bitunix", "api_key": "k", "api_secret": "s"},
    )
    assert r.status_code == 401
    r = c.get("/api/connections/exchange")
    assert r.status_code == 401
