"""English for Señales' visible text (Aldo, 2026-11-07, "Visible text only"):
the feed's "por qué", the analyst's prompt language, and every hop back to the
hub keep the language the hub launched with. Spanish stays exactly as before."""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from itertools import product
from typing import Any
from uuid import uuid4

import httpx
import pytest
from chalybcrypto_api.deps import set_store_for_tests
from chalybcrypto_api.feed import (
    FEED_TEXT_BANNED_EN,
    FEED_TEXTS,
    _text,
    compute_reading,
    localize_text,
    run_feed_tick,
    text_is_clean,
    wire,
)
from chalybcrypto_api.locale import hub_path, lang_from_accept_language, normalize_lang
from chalybcrypto_api.main import app
from chalybcrypto_api.sso import SESSION_COOKIE_NAME
from chalybcrypto_api.store import InMemoryStore
from chalybcrypto_llm import ClaudeAnalyst, ContinueBriefing, DailyDigest
from chalybcrypto_llm import analyst as analyst_mod
from chalybcrypto_shared import Side
from fastapi.testclient import TestClient

from tests.test_api_sso import _sign_launch_token
from tests.test_feed import NOW, FakeSource, _bearer, _series

SECRET = "test-shared-sso-secret-must-be-long-enough-32b"
SPANISH_WORDS = ("las ", " de ", "está", "últimas", "subió", "bajó", "estrategias", "dirección")


# ── language parsing ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,want",
    [("en", "en"), ("EN", "en"), ("en-US", "en"), ("en_GB", "en"), ("es", "es"),
     ("es-MX", "es"), ("fr", None), ("", None), (None, None), ("english", None)],
)
def test_normalize_lang(raw, want):
    assert normalize_lang(raw) == want


def test_accept_language_takes_the_first_supported():
    assert lang_from_accept_language("fr-FR,en-US;q=0.8,es;q=0.5") == "en"
    assert lang_from_accept_language("es-MX,es;q=0.9,en;q=0.8") == "es"
    assert lang_from_accept_language("de,fr") is None
    assert lang_from_accept_language(None) is None


def test_hub_path_prefixes_only_english():
    assert hub_path("/app/senales", "en") == "/en/app/senales"
    assert hub_path("/app/senales", "es") == "/app/senales"
    assert hub_path("/app/senales", None) == "/app/senales"
    assert hub_path("/app/senales", "fr") == "/app/senales"


# ── feed text ──────────────────────────────────────────────────────────────


def test_both_languages_have_the_same_sentences_and_placeholders():
    import re

    assert FEED_TEXTS["es"].keys() == FEED_TEXTS["en"].keys()
    for key in FEED_TEXTS["es"]:
        es_ph = sorted(re.findall(r"\{(\w+)\}", FEED_TEXTS["es"][key]))
        en_ph = sorted(re.findall(r"\{(\w+)\}", FEED_TEXTS["en"][key]))
        assert es_ph == en_ph, key


def _sample_values():
    return {"pct": "3.4", "adx": "27", "rsi": "71", "agree": "2", "total": "3"}


@pytest.mark.parametrize("key", sorted(FEED_TEXTS["es"]))
def test_every_stored_sentence_translates_to_its_english(key):
    es = _text("es", key, **_sample_values())
    en = _text("en", key, **_sample_values())
    assert localize_text(es, "en") == en
    assert localize_text(es, "es") == es  # Spanish untouched


def test_english_text_is_clean_and_has_no_spanish():
    for key, t in FEED_TEXTS["en"].items():
        s = t.format(**_sample_values())
        assert not any(p.search(s) for p in FEED_TEXT_BANNED_EN), key
        assert text_is_clean(s), key
        assert not any(w in s.lower() for w in SPANISH_WORDS), (key, s)


def test_unknown_text_is_left_alone():
    assert localize_text("Texto escrito a mano.", "en") == "Texto escrito a mano."
    # A near-miss (extra suffix) is not half-translated.
    tail = "El RSI está en 55: en una zona media. Extra."
    assert localize_text(tail, "en") == tail


@pytest.mark.parametrize("kind", ["up", "down", "flat"])
def test_real_readings_translate_completely(kind):
    """Real candles through compute_reading: every sentence it writes has an English."""
    r = compute_reading("BTC", _series(kind), NOW)
    row = {**r, "id": uuid4(), "created_at": NOW, "ref_price_mxn": Decimal(1),
           "why_short": r["why_short"], "why_bullets": r["why_bullets"]}
    es, en = wire(row), wire(row, "en")
    assert es == wire(row, "es")
    assert es["why"]["short"] == r["why_short"] and es["explanation"] == r["explanation"]
    texts = [en["explanation"], en["why"]["short"], *en["why"]["bullets"]]
    for t in texts:
        assert not any(w in t.lower() for w in SPANISH_WORDS), t
    # Everything but the text is the same.
    for k in ("id", "coin", "state", "confidence", "at", "ref_price_mxn"):
        assert es[k] == en[k]


def test_spanish_sentences_unchanged_from_before():
    """The stored Spanish is byte-identical to the templates before this change."""
    assert _text("es", "change_up", pct="1.2") == "En las últimas 24 horas subió 1.2%."
    assert _text("es", "adx_moderate", adx="20") == (
        "La fuerza de la tendencia (ADX) está en 20: es moderada."
    )
    assert _text("es", "explain_agree", agree=2, total=3) == (
        "2 de 3 estrategias de Señales coinciden en esta lectura."
    )


# ── /api/hub/v1/feed?lang= ─────────────────────────────────────────────────


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("CHALYB_SSO_SECRET", SECRET)
    store = InMemoryStore()
    set_store_for_tests(store)
    asyncio.run(run_feed_tick(store, FakeSource(), usd_mxn=Decimal(18), coins=("BTC", "SOL")))
    with TestClient(app) as c:
        yield c, store


def _texts(sig: dict) -> list[str]:
    return [sig["explanation"], sig["why"]["short"], *sig["why"]["bullets"]]


def test_feed_defaults_to_spanish_and_serves_english_on_request(api):
    c, store = api
    h = _bearer(uuid4())
    default = c.get("/api/hub/v1/feed", headers=h).json()["signals"]
    es = c.get("/api/hub/v1/feed?lang=es", headers=h).json()["signals"]
    en = c.get("/api/hub/v1/feed?lang=en", headers=h).json()["signals"]
    assert default == es
    stored = {str(r["id"]): r for r in store._feed}
    for s in es:
        assert s["why"]["short"] == stored[s["id"]]["why_short"]
    for s in en:
        for t in _texts(s):
            assert not any(w in t.lower() for w in SPANISH_WORDS), t
    assert [s["id"] for s in en] == [s["id"] for s in es]


def test_feed_language_from_accept_language_and_query_wins(api):
    c, _ = api
    h = _bearer(uuid4())
    by_header = c.get("/api/hub/v1/feed", headers={**h, "Accept-Language": "en-US,en;q=0.9"})
    query_wins = c.get("/api/hub/v1/feed?lang=es", headers={**h, "Accept-Language": "en"})
    unknown = c.get("/api/hub/v1/feed?lang=fr", headers=h)
    es = c.get("/api/hub/v1/feed", headers=h).json()
    en = c.get("/api/hub/v1/feed?lang=en", headers=h).json()
    assert by_header.json() == en
    assert query_wins.json() == es
    assert unknown.status_code == 200 and unknown.json() == es


def test_feed_entry_in_english(api, monkeypatch):
    c, store = api

    async def none():
        return None

    monkeypatch.setattr("chalybcrypto_api.fx.usd_mxn", none)
    entry_id = store._feed[0]["id"]
    r = c.get(f"/api/hub/v1/feed/{entry_id}?lang=en", headers=_bearer(uuid4()))
    assert r.status_code == 200
    for t in _texts(r.json()["signal"]):
        assert not any(w in t.lower() for w in SPANISH_WORDS), t


# ── links back to the hub ──────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("CHALYB_SSO_SECRET", SECRET)
    monkeypatch.setenv("CHALYBCRYPTO_AUTH", "jwt")
    monkeypatch.setenv("CHALYBCRYPTO_ENV", "dev")
    monkeypatch.setenv("CHALYB_BASE_URL", "https://www.chalyb.com")
    with TestClient(app) as c:
        yield c


def _launch(lang: str | None) -> str:
    token = _sign_launch_token(
        {"user_id": str(uuid4()), "email": "u@example.com", "tenant_id": "", "tier": "pro",
         "exp": int(time.time()) + 60}
    )
    return f"/auth/sso?token={token}" + (f"&lang={lang}" if lang is not None else "")


@pytest.mark.parametrize("lang,cookie", [("en", "en"), ("es", "es"), ("en-US", "en")])
def test_sso_remembers_the_hub_language(client, lang, cookie):
    r = client.get(_launch(lang), follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/dashboard"
    assert client.cookies.get("chalyb_lang") == cookie
    assert client.cookies.get(SESSION_COOKIE_NAME)


@pytest.mark.parametrize("lang", [None, "fr"])
def test_sso_without_a_known_language_sets_no_language(client, lang):
    client.get(_launch(lang), follow_redirects=False)
    assert client.cookies.get("chalyb_lang") is None


@pytest.mark.parametrize("lang", [None, "fr"])
def test_spanish_launch_drops_an_old_english_choice(client, monkeypatch, lang):
    """The hub sends `lang` only for English, so no lang means Spanish now."""
    monkeypatch.setenv("CHALYBCRYPTO_REDIRECT_TO_HUB", "1")
    client.get(_launch("en"), follow_redirects=False)
    assert client.cookies.get("chalyb_lang") == "en"
    r = client.get(_launch(lang), follow_redirects=False)
    assert "chalyb_lang=" in r.headers.get("set-cookie", "")
    assert client.cookies.get("chalyb_lang") is None
    r = client.get("/dashboard", follow_redirects=False)
    assert r.headers["location"] == "https://www.chalyb.com/app/senales"


def test_lang_cookie_is_httponly(client):
    r = client.get(_launch("en"), follow_redirects=False)
    lang_cookie = [c for c in r.headers.get_list("set-cookie") if c.startswith("chalyb_lang=")]
    assert lang_cookie and "httponly" in lang_cookie[0].lower()


def test_overlong_lang_is_ignored_not_rejected(client, monkeypatch):
    monkeypatch.setenv("CHALYBCRYPTO_REDIRECT_TO_HUB", "1")
    r = client.get("/dashboard?lang=" + "x" * 40, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "https://www.chalyb.com/app/senales"


@pytest.mark.parametrize("flow", ["english", "spanish", "none"])
def test_redirect_to_hub_keeps_the_sso_language(client, monkeypatch, flow):
    monkeypatch.setenv("CHALYBCRYPTO_REDIRECT_TO_HUB", "1")
    client.get(_launch({"english": "en", "spanish": "es", "none": None}[flow]),
               follow_redirects=False)
    want = {"english": "https://www.chalyb.com/en/app/senales"}.get(
        flow, "https://www.chalyb.com/app/senales"
    )
    for path in ("/dashboard", "/"):
        r = client.get(path, follow_redirects=False)
        assert r.headers["location"] == want, (flow, path)


def test_query_lang_beats_the_cookie(client, monkeypatch):
    monkeypatch.setenv("CHALYBCRYPTO_REDIRECT_TO_HUB", "1")
    client.cookies.set("chalyb_lang", "en")
    r = client.get("/dashboard?lang=es", follow_redirects=False)
    assert r.headers["location"] == "https://www.chalyb.com/app/senales"
    client.cookies.clear()
    r = client.get("/?lang=en", follow_redirects=False)
    assert r.headers["location"] == "https://www.chalyb.com/en/app/senales"


@pytest.mark.parametrize("lang,want", [
    ("en", "https://www.chalyb.com/auth/launch/chalybcrypto?lang=en"),
    ("es", "https://www.chalyb.com/auth/launch/chalybcrypto"),
    (None, "https://www.chalyb.com/auth/launch/chalybcrypto"),
])
def test_signed_out_sign_in_keeps_the_language(client, lang, want):
    if lang:
        client.cookies.set("chalyb_lang", lang)
    r = client.get("/dashboard", follow_redirects=False)
    assert r.headers["location"] == want


# ── analyst prompt language ────────────────────────────────────────────────


def _capture() -> tuple[ClaudeAnalyst, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={
            "model": "claude-haiku-4-5", "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ClaudeAnalyst(api_key="k", client=client), seen


async def test_spanish_prompts_are_byte_identical():
    analyst, seen = _capture()
    from datetime import UTC, datetime

    from tests.test_llm_analyst import _snapshot

    try:
        await analyst.continue_briefing(
            ContinueBriefing(pair="BTCUSDT", side=Side.LONG, current_unrealized_net=Decimal(1)),
            _snapshot(),
        )
        await analyst.daily_digest(
            DailyDigest(day=datetime(2026, 11, 7, tzinfo=UTC), trade_count=1, net_pnl=Decimal(0))
        )
    finally:
        await analyst.aclose()
    assert seen[0]["system"][0]["text"] == analyst_mod._CONTINUE_SYSTEM
    assert seen[1]["system"][0]["text"] == analyst_mod._DIGEST_SYSTEM


async def test_english_prompts_ask_for_english():
    from datetime import UTC, datetime

    from tests.test_llm_analyst import _decision, _signal, _snapshot

    analyst, seen = _capture()
    try:
        await analyst.write_thesis(_signal(), _snapshot(), _decision(), lang="en")
        await analyst.continue_briefing(
            ContinueBriefing(pair="BTCUSDT", side=Side.LONG, current_unrealized_net=Decimal(1)),
            _snapshot(),
            lang="en",
        )
        await analyst.daily_digest(
            DailyDigest(day=datetime(2026, 11, 7, tzinfo=UTC), trade_count=1, net_pnl=Decimal(0)),
            lang="en",
        )
    finally:
        await analyst.aclose()
    for body in seen:
        text = body["system"][0]["text"]
        assert "Write in English." in text
        assert "Spanish" not in text
        assert body["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_lang_combinations_cover_every_prompt():
    for system, lang in product(
        (analyst_mod._THESIS_SYSTEM, analyst_mod._CONTINUE_SYSTEM, analyst_mod._DIGEST_SYSTEM),
        ("es", "en"),
    ):
        out = analyst_mod._system_for(system, lang)
        assert (out == system) == (lang == "es")
