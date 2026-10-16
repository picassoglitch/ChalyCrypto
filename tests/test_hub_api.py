"""/api/hub/v1 — the hub's server-to-server API (hub Bearer token only)."""

from __future__ import annotations

import base64
import hmac
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from uuid import UUID

import jwt
import pytest
from chalybcrypto_api.consumption import get_hub_client
from chalybcrypto_api.deps import set_store_for_tests
from chalybcrypto_api.hub_auth import HUB_TOKEN_AUDIENCE, HUB_TOKEN_MAX_TTL_SECONDS
from chalybcrypto_api.main import app, hub_app
from chalybcrypto_api.sso import mint_session_jwt
from chalybcrypto_api.store import InMemoryStore
from chalybcrypto_shared import MarginType, Side, Signal, dedup_hash
from fastapi.testclient import TestClient

from tests.test_consumption_admission import FakeHub

SECRET = "test-shared-sso-secret-must-be-long-enough-32b"
ALICE = UUID("aaaaaaaa-0000-4000-8000-000000000001")
BOB = UUID("bbbbbbbb-0000-4000-8000-000000000002")
V1 = "/api/hub/v1"


def _sign(payload: dict, *, secret: str = SECRET) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(hmac.new(secret.encode(), body.encode(), sha256).digest())
    return f"{body}.{sig.rstrip(b'=').decode()}"


def _claims(user: UUID = ALICE, **over) -> dict:
    c = {
        "user_id": str(user),
        "email": f"{str(user)[:4]}@example.com",
        "tenant_id": "",
        "tier": "pro",
        "exp": int(time.time()) + 120,
        "aud": HUB_TOKEN_AUDIENCE,
    }
    c.update(over)
    return {k: v for k, v in c.items() if v is not ...}


def bearer(user: UUID = ALICE, **over) -> dict:
    return {"Authorization": f"Bearer {_sign(_claims(user, **over))}"}


@pytest.fixture
def store():
    s = InMemoryStore()
    set_store_for_tests(s)
    return s


@pytest.fixture
def client(monkeypatch, store):
    monkeypatch.setenv("CHALYB_SSO_SECRET", SECRET)
    monkeypatch.delenv("CHALYBCRYPTO_REDIRECT_TO_HUB", raising=False)
    with TestClient(app) as c:
        yield c


def _signal(pair: str = "BTCUSDT") -> Signal:
    return Signal(
        pair=pair,
        side=Side.LONG,
        strategy="ema_adx_trend",
        entry=Decimal("60000"),
        stop_loss=Decimal("59700"),
        take_profits=[Decimal("60900")],
        leverage=Decimal("10"),
        margin_type=MarginType.ISOLATED,
        timeframe="5m",
        thesis_tags=[],
        source="scanner",
        dedup_hash=dedup_hash(pair, "long"),
        created_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )


# ── happy path ─────────────────────────────────────────────────────────────


def test_me_returns_the_token_subject(client):
    r = client.get(f"{V1}/me", headers=bearer(tier="vip"))
    assert r.status_code == 200, r.text
    assert r.json() == {"user_id": str(ALICE), "email": "aaaa@example.com", "tier": "vip"}


def test_engine_routes_are_served_under_v1_with_the_same_handlers(client):
    h = bearer()
    assert client.get(f"{V1}/signals", headers=h).json() == []
    assert client.get(f"{V1}/mode", headers=h).json()["mode"] == "paper"
    keys = {s["key"] for s in client.get(f"{V1}/strategies", headers=h).json()}
    assert keys == {"ema_adx_trend", "vwap_rsi_meanrev", "fvg_ob"}
    assert client.get(f"{V1}/approvals", headers=h).json() == []
    assert client.get(f"{V1}/trades", headers=h).json() == []


async def test_data_is_scoped_to_the_token_user(client, store):
    await store.add_parsed_signal(user_id=ALICE, signal=_signal())
    a = client.get(f"{V1}/signals", headers=bearer(ALICE)).json()
    b = client.get(f"{V1}/signals", headers=bearer(BOB)).json()
    assert len(a) == 1 and a[0]["pair"] == "BTCUSDT"
    assert b == []


async def test_approval_of_another_user_is_not_found(client, store):
    sig = _signal()
    from chalybcrypto_shared import Mode, TradeDecision

    dec = TradeDecision(
        signal_id=sig.id,
        mode=Mode.SEMI_AUTO,
        approved=True,
        reason="ok",
        intended_qty=Decimal("0.01"),
        ev_net_bps=Decimal("12"),
        liquidation_distance_bps=Decimal("900"),
        idempotency_key="k1",
        decided_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    row = await store.add_approval(user_id=ALICE, signal=sig, decision=dec)
    r = client.post(
        f"{V1}/approvals/{row['id']}/decision", headers=bearer(BOB), json={"action": "reject"}
    )
    assert r.status_code == 404
    r = client.post(
        f"{V1}/approvals/{row['id']}/decision", headers=bearer(ALICE), json={"action": "reject"}
    )
    assert r.status_code == 200, r.text


# ── refusals ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer "},
        {"Authorization": "Bearer nodot"},
        {"Authorization": "Bearer a.b.c"},
        {"Authorization": "Bearer .sig"},
    ],
)
def test_missing_or_malformed_token_is_401(client, headers):
    assert client.get(f"{V1}/signals", headers=headers).status_code == 401


@pytest.mark.parametrize(
    "over",
    [
        {"aud": ...},  # a plain launch token
        {"aud": "chalybcrypto-session"},
        {"aud": ["chalybcrypto-hub-api"]},
        {"exp": int(time.time()) - 1},
        {"exp": int(time.time()) + HUB_TOKEN_MAX_TTL_SECONDS + 60},
        {"exp": "9999999999"},
        {"exp": True},
        {"exp": ...},
        {"user_id": "not-a-uuid"},
        {"user_id": ...},
        {"email": 123},
    ],
)
def test_bad_claims_are_401(client, over):
    assert client.get(f"{V1}/signals", headers=bearer(**over)).status_code == 401


@pytest.mark.parametrize("email", ["", ...])
def test_email_is_optional(client, email):
    r = client.get(f"{V1}/me", headers=bearer(email=email))
    assert r.status_code == 200
    assert r.json()["email"] == ""


def test_wrong_secret_is_401(client):
    tok = _sign(_claims(), secret="another-secret-of-the-same-length-000000")
    r = client.get(f"{V1}/signals", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401


def test_tampered_body_is_401(client):
    tok = _sign(_claims(ALICE))
    _, sig = tok.split(".")
    forged_body = (
        base64.urlsafe_b64encode(json.dumps(_claims(BOB)).encode()).rstrip(b"=").decode()
    )
    r = client.get(f"{V1}/me", headers={"Authorization": f"Bearer {forged_body}.{sig}"})
    assert r.status_code == 401


def test_no_secret_configured_fails_closed(client, monkeypatch):
    monkeypatch.delenv("CHALYB_SSO_SECRET")
    assert client.get(f"{V1}/signals", headers=bearer()).status_code == 500


def test_stub_header_and_session_cookie_are_not_accepted_on_v1(client, monkeypatch):
    monkeypatch.setenv("CHALYBCRYPTO_AUTH", "stub")
    r = client.get(f"{V1}/signals", headers={"X-User-Id": str(ALICE)})
    assert r.status_code == 401
    monkeypatch.setenv("CHALYBCRYPTO_AUTH", "jwt")
    client.cookies.set("nxc_session", mint_session_jwt(user_id=str(ALICE), email="a@x.co"))
    r = client.get(f"{V1}/signals")
    assert r.status_code == 401
    client.cookies.clear()


def test_supabase_jwt_is_not_accepted_on_v1(client, monkeypatch):
    legacy = "legacy-supabase-secret-legacy-supabase-secret"
    monkeypatch.setenv("CHALYBCRYPTO_SUPABASE_JWT_SECRET", legacy)
    tok = jwt.encode(
        {"sub": str(ALICE), "aud": "authenticated", "exp": int(time.time()) + 60},
        legacy,
        algorithm="HS256",
    )
    r = client.get(f"{V1}/signals", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 401


def test_hub_token_is_not_a_bearer_on_the_old_api(client, monkeypatch):
    monkeypatch.setenv("CHALYBCRYPTO_AUTH", "jwt")
    monkeypatch.setenv("CHALYBCRYPTO_SUPABASE_JWT_SECRET", "x" * 40)
    assert client.get("/api/signals", headers=bearer()).status_code == 401


def test_hub_token_cannot_become_a_browser_session(client):
    tok = _sign(_claims())
    r = client.get(f"/auth/sso?token={tok}", follow_redirects=False)
    assert r.status_code == 401
    assert "nxc_session" not in r.headers.get("set-cookie", "")


def test_old_api_still_works_in_stub_mode(client, monkeypatch):
    monkeypatch.setenv("CHALYBCRYPTO_AUTH", "stub")
    assert client.get("/api/signals", headers={"X-User-Id": str(ALICE)}).status_code == 200


def test_excluded_routes_are_not_on_v1(client):
    h = bearer()
    assert client.put(f"{V1}/fee-schedules", headers=h, json=[]).status_code == 405
    assert client.get(f"{V1}/fee-schedules", headers=h).status_code == 200
    assert client.get(f"{V1}/stream", headers=h).status_code == 404


def test_v1_has_no_public_docs(client):
    assert client.get(f"{V1}/docs").status_code == 404
    assert client.get(f"{V1}/openapi.json").status_code == 404


# ── usage admission keeps working through v1 ──────────────────────────────


@pytest.mark.parametrize(("reason", "code"), [("no_tokens", 402), ("jobs_cap", 429), ("storage_full", 413)])
def test_backtest_refusal_reaches_the_hub_with_reason(client, reason, code):
    hub = FakeHub({"ok": True, "allowed": False, "reason": reason})
    hub_app.dependency_overrides[get_hub_client] = lambda: hub.client
    try:
        r = client.post(
            f"{V1}/backtests", headers=bearer(), json={"strategy": "ema_adx_trend", "pair": "BTCUSDT"}
        )
    finally:
        hub_app.dependency_overrides.pop(get_hub_client, None)
    assert r.status_code == code
    assert r.json()["detail"]["reason"] == reason
    assert hub.calls[0][1]["external_user_id"] == str(ALICE)


def test_backtest_admitted_through_v1(client):
    hub = FakeHub()
    hub_app.dependency_overrides[get_hub_client] = lambda: hub.client
    try:
        r = client.post(
            f"{V1}/backtests", headers=bearer(), json={"strategy": "fvg_ob", "pair": "ETHUSDT"}
        )
    finally:
        hub_app.dependency_overrides.pop(get_hub_client, None)
    assert r.status_code == 200, r.text
    assert r.json()["optimistic"] is True
    assert r.json()["status"] == "done"
    runs = client.get(f"{V1}/backtests", headers=bearer()).json()
    assert runs[0]["job_id"] == r.json()["job_id"]
    # Another user can't see it.
    assert client.get(f"{V1}/backtests", headers=bearer(BOB)).json() == []
    assert client.get(f"{V1}/backtests/{r.json()['job_id']}", headers=bearer(BOB)).status_code == 404
    assert hub.paths() == ["/admit", "/settle"]


# ── signal prefs ───────────────────────────────────────────────────────────

PREFS = {
    "coins": ["BTC", "ETH", "BTC"],
    "channels": ["app", "email"],
    "timeframe": "day",
    "quietHours": True,
    "from": "08:00",
    "to": "22:00",
    "days": "weekdays",
    "dailySummary": False,
    "format": "short",
}


def test_signal_prefs_round_trip_and_isolation(client):
    assert client.get(f"{V1}/signal-prefs", headers=bearer()).json() is None
    r = client.put(f"{V1}/signal-prefs", headers=bearer(), json=PREFS)
    assert r.status_code == 200, r.text
    assert r.json()["coins"] == ["BTC", "ETH"]
    assert r.json()["from"] == "08:00"
    got = client.get(f"{V1}/signal-prefs", headers=bearer()).json()
    assert got == r.json()
    assert client.get(f"{V1}/signal-prefs", headers=bearer(BOB)).json() is None


def test_signal_prefs_minimal_shape(client):
    body = {"coins": ["SOL"], "channels": ["app"], "timeframe": "week", "quietHours": False}
    r = client.put(f"{V1}/signal-prefs", headers=bearer(), json=body)
    assert r.status_code == 200
    assert r.json() == body


@pytest.mark.parametrize(
    "patch",
    [
        {"risk_profile": "aggressive"},
        {"balance": 1000},
        {"channels": ["telegram"]},
        {"timeframe": "month"},
        {"from": "25:00"},
        {"coins": ["btc"]},
        {"coins": ["X" * 16]},
        {"coins": [f"C{i}" for i in range(51)]},
    ],
)
def test_signal_prefs_refuse_unknown_or_bad_fields(client, patch):
    r = client.put(f"{V1}/signal-prefs", headers=bearer(), json={**PREFS, **patch})
    assert r.status_code == 422


def test_signal_prefs_need_the_hub_token(client):
    assert client.put(f"{V1}/signal-prefs", json=PREFS).status_code == 401


# ── user pages → hub (env flag) ────────────────────────────────────────────


def test_dashboard_served_when_flag_off(client):
    r = client.get("/dashboard")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


@pytest.mark.parametrize("val", ["1", "true", "yes"])
def test_dashboard_redirects_to_hub_when_flag_on(client, monkeypatch, val):
    monkeypatch.setenv("CHALYBCRYPTO_REDIRECT_TO_HUB", val)
    monkeypatch.setenv("CHALYB_BASE_URL", "https://www.chalyb.com/")
    for path in ("/dashboard", "/"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (302, 307)
        assert r.headers["location"] == "https://www.chalyb.com/app/senales"
    # Backend URLs keep working.
    assert client.get("/api/health").status_code == 200
    assert client.get(f"{V1}/me", headers=bearer()).status_code == 200


def test_redirect_flag_defaults_to_chalyb(client, monkeypatch):
    monkeypatch.setenv("CHALYBCRYPTO_REDIRECT_TO_HUB", "1")
    monkeypatch.delenv("CHALYB_BASE_URL", raising=False)
    r = client.get("/dashboard", follow_redirects=False)
    assert r.headers["location"] == "https://www.chalyb.com/app/senales"


def test_redirect_flag_off_values(client, monkeypatch):
    monkeypatch.setenv("CHALYBCRYPTO_REDIRECT_TO_HUB", "0")
    assert client.get("/dashboard", follow_redirects=False).status_code == 200
