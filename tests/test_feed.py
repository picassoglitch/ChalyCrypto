"""Señales shared feed: market-data-only readings, no trade setups, clean text,
change-only inserts, and the /api/hub/v1/feed contract."""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from chalybcrypto_api import fx
from chalybcrypto_api.deps import set_store_for_tests
from chalybcrypto_api.feed import (
    FEED_TEXT_BANNED,
    compute_reading,
    run_feed_tick,
    text_is_clean,
    wire,
)
from chalybcrypto_api.main import app
from chalybcrypto_api.store import InMemoryStore
from fastapi.testclient import TestClient

from tests.conftest import synthetic_klines

SECRET = "test-shared-sso-secret-must-be-long-enough-32b"
NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)
SETUP_KEYS = {"entry", "stop_loss", "take_profits", "leverage", "qty", "side", "user_id"}

# The hub's SIGNAL_TEXT_BANNED + §8 list (src/lib/guardrails/signals.ts,
# TOOLS-SPEC §8), as the hub tests them.
HUB_BANNED = [
    "te conviene", "deberías", "tu cartera", "tus ganancias", "si ya ganaste",
    "garantizado", "seguro que", "sin riesgo", "ganancia asegurada", "rendimiento de",
    "% de ganancia", "objetivo de precio", "precio objetivo", "copiar", "invierte ahora",
    "no te lo pierdas",
]


def _series(kind: str, n: int = 200):
    ks = synthetic_klines(n, step_minutes=60)
    if kind == "flat":
        return ks
    from chalybcrypto_shared import Kline

    out, price = [], 100.0
    for i, k in enumerate(ks):
        step = 0.006 if kind == "up" else -0.006
        o = price
        c = o * (1 + step + (0.002 if i % 3 == 0 else -0.001) * (1 if kind == "up" else -1))
        out.append(
            Kline(
                open_time=k.open_time,
                close_time=k.close_time,
                open=Decimal(f"{o:.4f}"),
                close=Decimal(f"{c:.4f}"),
                high=Decimal(f"{max(o, c) * 1.002:.4f}"),
                low=Decimal(f"{min(o, c) * 0.998:.4f}"),
                volume=Decimal(10),
            )
        )
        price = c
    return out


@pytest.mark.parametrize("kind", ["up", "down", "flat"])
def test_reading_is_general_and_clean(kind):
    r = compute_reading("BTC", _series(kind), NOW)
    assert r["state"] in {"buy", "sell", "wait"}
    assert not (SETUP_KEYS & set(r)), "no trade setup leaves the feed"
    assert (r["confidence"] == "none") == (r["state"] == "wait")
    assert len(r["why_bullets"]) == 3
    texts = [r["why_short"], r["explanation"], *r["why_bullets"]]
    assert text_is_clean(*texts)
    for t in texts:
        for phrase in HUB_BANNED:
            assert phrase not in t.lower(), (phrase, t)


def test_a_steady_rise_reads_as_buy_and_a_steady_fall_as_sell():
    up = compute_reading("ETH", _series("up"), NOW)
    down = compute_reading("ETH", _series("down"), NOW)
    assert up["state"] in {"buy", "wait"} and down["state"] in {"sell", "wait"}
    assert "subió" in up["why_bullets"][0]
    assert "bajó" in down["why_bullets"][0]


def test_banned_list_covers_the_hub_phrases():
    for phrase in HUB_BANNED:
        assert not text_is_clean(f"Hoy {phrase} algo"), phrase


def test_too_few_candles_is_an_error():
    with pytest.raises(ValueError):
        compute_reading("BTC", _series("up")[:30], NOW)


class FakeSource:
    def __init__(self, kind="up", fail=()):
        self.kind, self.fail = kind, set(fail)

    async def klines(self, pair, interval, *, limit=100):
        if pair[:-4] in self.fail:
            raise RuntimeError("venue down")
        return list(reversed(_series(self.kind)))  # newest first, like Bitunix


async def test_tick_inserts_on_change_or_after_24h_only():
    store = InMemoryStore()
    coins = ("BTC", "ETH")
    first = await run_feed_tick(store, FakeSource(), usd_mxn=Decimal("18.4"), now=NOW, coins=coins)
    assert first == {"BTC": "inserted", "ETH": "inserted"}
    # In-memory rows get the insert time; make it NOW for the window test.
    for r in store._feed:
        r["created_at"] = NOW
    again = await run_feed_tick(store, FakeSource(), usd_mxn=None, now=NOW + timedelta(hours=1), coins=coins)
    assert again == {"BTC": "unchanged", "ETH": "unchanged"}
    later = await run_feed_tick(store, FakeSource(), usd_mxn=None, now=NOW + timedelta(hours=25), coins=coins)
    assert later == {"BTC": "inserted", "ETH": "inserted"}
    row = store._feed[0]
    assert row["ref_price_mxn"] == (row["ref_price_usd"] * Decimal("18.4")).quantize(Decimal("0.01"))
    assert store._feed[-1]["ref_price_mxn"] is None  # no rate → no guessed MXN


async def test_one_coin_failing_does_not_stop_the_others():
    store = InMemoryStore()
    out = await run_feed_tick(store, FakeSource(fail={"ETH"}), usd_mxn=None, now=NOW, coins=("BTC", "ETH"))
    assert out == {"BTC": "inserted", "ETH": "error"}


# ── /api/hub/v1/feed ───────────────────────────────────────────────────────


def _bearer(user: UUID) -> dict:
    claims = {
        "user_id": str(user), "email": "", "tenant_id": "", "tier": "pro",
        "exp": int(time.time()) + 60, "aud": "chalybcrypto-hub-api",
    }
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    sig = base64.urlsafe_b64encode(hmac.new(SECRET.encode(), body.encode(), sha256).digest())
    return {"Authorization": f"Bearer {body}.{sig.rstrip(b'=').decode()}"}


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("CHALYB_SSO_SECRET", SECRET)
    store = InMemoryStore()
    set_store_for_tests(store)
    asyncio.run(run_feed_tick(store, FakeSource(), usd_mxn=Decimal("18"), coins=("BTC", "SOL")))
    with TestClient(app) as c:
        yield c, store


def test_feed_is_the_same_for_every_user_and_plan(api):
    c, _ = api
    a = c.get("/api/hub/v1/feed?plan=PRO", headers=_bearer(uuid4())).json()
    b = c.get("/api/hub/v1/feed?plan=VIP", headers=_bearer(uuid4())).json()
    assert a["signals"] == b["signals"]
    assert {s["coin"] for s in a["signals"]} == {"BTC", "SOL"}
    for s in a["signals"]:
        assert set(s) == {"id", "coin", "state", "confidence", "explanation", "at", "ref_price_mxn", "why"}
    assert c.get("/api/hub/v1/feed").status_code == 401
    assert c.get("/api/hub/v1/feed?plan=ROOT", headers=_bearer(uuid4())).status_code == 422


def test_feed_entry_with_past_price_in_mxn(api, monkeypatch):
    c, store = api
    entry_id = store._feed[0]["id"]

    async def rate():
        return Decimal("18.5")

    async def fake_klines(self, pair, interval, *, limit=100, start=None, end=None):
        assert (interval, limit) == ("4h", 42)
        return list(reversed(synthetic_klines(limit, step_minutes=240)))

    from chalybcrypto_connectors.bitunix import BitunixConnector

    monkeypatch.setattr("chalybcrypto_api.fx.usd_mxn", rate)
    monkeypatch.setattr(BitunixConnector, "klines", fake_klines)
    r = c.get(f"/api/hub/v1/feed/{entry_id}?range=7d", headers=_bearer(uuid4()))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["signal"]["id"] == str(entry_id)
    ats = [p["at"] for p in body["series"]]
    assert len(ats) == 42 and ats == sorted(ats)
    assert c.get(f"/api/hub/v1/feed/{uuid4()}", headers=_bearer(uuid4())).status_code == 404
    assert c.get(f"/api/hub/v1/feed/{entry_id}?range=5y", headers=_bearer(uuid4())).status_code == 422


def test_feed_entry_without_mxn_rate_has_no_series(api, monkeypatch):
    c, store = api

    async def none():
        return None

    monkeypatch.setattr("chalybcrypto_api.fx.usd_mxn", none)
    r = c.get(f"/api/hub/v1/feed/{store._feed[0]['id']}", headers=_bearer(uuid4()))
    assert r.json()["series"] == []


def test_wire_has_no_internals():
    row = {
        "id": uuid4(), "coin": "BTC", "state": "buy", "confidence": "high", "explanation": "x",
        "created_at": NOW, "ref_price_mxn": Decimal("1.5"), "why_short": "s", "why_bullets": ["a"],
        "strategies": ["ema_adx_trend"], "ref_price_usd": Decimal(1), "pair": "BTCUSDT",
    }
    w = wire(row)
    assert "strategies" not in w and "ref_price_usd" not in w and w["ref_price_mxn"] == 1.5


async def test_fx_reads_bitso_and_bounds(monkeypatch):
    import httpx

    fx.reset_cache_for_tests()

    def handler(req):
        return httpx.Response(200, json={"payload": {"last": "18.40"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        assert await fx.usd_mxn(c) == Decimal("18.40")
    fx.reset_cache_for_tests()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"payload": {"last": "0.01"}}))
    ) as c:
        assert await fx.usd_mxn(c) is None
    fx.reset_cache_for_tests()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500, text="x"))) as c:
        assert await fx.usd_mxn(c) is None
    fx.reset_cache_for_tests()


async def test_feed_job_needs_a_database(monkeypatch):
    from chalybcrypto_api import feed_job

    monkeypatch.delenv("CHALYBCRYPTO_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert await feed_job.main() == 2


def test_no_banned_regex_is_broken():
    assert all(p.pattern for p in FEED_TEXT_BANNED)
