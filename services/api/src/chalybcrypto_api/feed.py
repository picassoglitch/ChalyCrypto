"""Señales shared feed: one general reading per coin, the same for everyone.

What the Chalyb hub shows in /app/senales (REVISION-LEGAL C1 in the hub):
  * computed from MARKET DATA ONLY — the engine's strategies on Bitunix 1h
    candles. No account, balance, holding, risk profile or user anywhere.
  * no trade setup: no entry, stop, take-profit or leverage leaves this module.
    Only the direction the strategies agree on (buy / sell / wait), how many
    agree, and a plain-language "por qué" about the PRICE (Spanish stored,
    English rendered at read time when the hub asks for `lang=en`).
  * text is written from templates and checked against the hub's banned
    phrases (FEED_TEXT_BANNED, mirrored from the hub's SIGNAL_TEXT_BANNED).

A tick (feed_job.py, every 15 min on Cloud Run Jobs) reads each coin and
writes a new row only when the reading changes, or the last one is 24 h old.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from chalybcrypto_engine.strategy.base import StrategyContext
from chalybcrypto_engine.strategy.indicators import adx, rsi
from chalybcrypto_shared import Kline, MarketSnapshot, Side

from .strategy_catalog import CATALOG, build

log = logging.getLogger(__name__)

# The hub's coin list (src/lib/tools/senales-coins.ts), quoted in USDT.
FEED_COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "BNB", "LTC")
FEED_INTERVAL = "1h"
FEED_BARS = 200  # Bitunix's max per request: ~8 days of 1h candles
REFRESH_AFTER = timedelta(hours=24)

# Mirror of the hub's SIGNAL_TEXT_BANNED (src/lib/guardrails/signals.ts) plus
# its §8 list: no advice, no promises, no talk about the person's money.
FEED_TEXT_BANNED = [
    re.compile(p, re.I)
    for p in (
        r"te conviene",
        r"deber[ií]as",
        r"tu cartera",
        r"tus ganancias",
        r"si ya ganaste",
        r"garantizad[oa]",
        r"seguro que",
        r"sin riesgo",
        r"ganancia asegurada",
        r"rendimiento de",
        r"% de ganancia",
        r"objetivo de precio",
        r"precio objetivo",
        r"\bcopiar\b",
        r"invierte ahora",
        r"no te lo pierdas",
        r"compra ya|vende ya",
        r"apalancamiento|stop loss|take profit",
    )
]


class FeedStore(Protocol):
    async def latest_feed_entry(self, *, coin: str) -> dict | None: ...
    async def add_feed_entry(self, *, entry: dict) -> dict: ...


class KlineSource(Protocol):
    async def klines(self, pair: str, interval: str, *, limit: int = 100) -> list[Kline]: ...


def text_is_clean(*texts: str) -> bool:
    return not any(p.search(t) for t in texts for p in FEED_TEXT_BANNED)


def _pct(v: Decimal) -> str:
    return f"{abs(v):.1f}"


# Rows are stored in Spanish; English is rendered at read time from the same
# keys so old rows translate too ("Visible text only", Aldo 2026-11-07).
FEED_TEXTS: dict[str, dict[str, str]] = {
    "es": {
        "short_buy": "Las lecturas del precio en las últimas horas apuntan hacia arriba.",
        "short_sell": "Las lecturas del precio en las últimas horas apuntan hacia abajo.",
        "short_wait": "No hay una dirección clara por ahora. Te avisamos cuando la haya.",
        "change_flat": "En las últimas 24 horas casi no se movió.",
        "change_up": "En las últimas 24 horas subió {pct}%.",
        "change_down": "En las últimas 24 horas bajó {pct}%.",
        "adx_none": "Todavía no hay datos para medir la fuerza de la tendencia.",
        "adx_high": "La fuerza de la tendencia (ADX) está en {adx}: es alta.",
        "adx_moderate": "La fuerza de la tendencia (ADX) está en {adx}: es moderada.",
        "adx_low": "La fuerza de la tendencia (ADX) está en {adx}: es baja.",
        "rsi_none": "Todavía no hay datos para el RSI.",
        "rsi_high": "El RSI está en {rsi}: subió rápido en poco tiempo.",
        "rsi_low": "El RSI está en {rsi}: bajó rápido en poco tiempo.",
        "rsi_mid": "El RSI está en {rsi}: en una zona media.",
        "explain_split": "Las estrategias de Señales no coinciden en una dirección.",
        "explain_none": "Ninguna estrategia de Señales ve una dirección clara.",
        "explain_agree": "{agree} de {total} estrategias de Señales coinciden en esta lectura.",
    },
    "en": {
        "short_buy": "Price readings over the last few hours point up.",
        "short_sell": "Price readings over the last few hours point down.",
        "short_wait": "No clear direction for now. We'll let you know when there is one.",
        "change_flat": "It barely moved in the last 24 hours.",
        "change_up": "In the last 24 hours it rose {pct}%.",
        "change_down": "In the last 24 hours it fell {pct}%.",
        "adx_none": "There is no data yet to measure the trend's strength.",
        "adx_high": "Trend strength (ADX) is at {adx}: high.",
        "adx_moderate": "Trend strength (ADX) is at {adx}: moderate.",
        "adx_low": "Trend strength (ADX) is at {adx}: low.",
        "rsi_none": "There is no RSI data yet.",
        "rsi_high": "RSI is at {rsi}: it rose fast in a short time.",
        "rsi_low": "RSI is at {rsi}: it fell fast in a short time.",
        "rsi_mid": "RSI is at {rsi}: in a middle zone.",
        "explain_split": "The Signals strategies don't agree on a direction.",
        "explain_none": "No Signals strategy sees a clear direction.",
        "explain_agree": "{agree} of {total} Signals strategies agree on this reading.",
    },
}

# English counterpart of FEED_TEXT_BANNED: no advice, no promises, no talk about
# the person's money, in the English text either.
FEED_TEXT_BANNED_EN = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"you should",
        r"your portfolio",
        r"your (gains|profits)",
        r"guarantee",
        r"risk[- ]free",
        r"sure (thing|bet)",
        r"price target|target price",
        r"\bcopy\b",
        r"invest now",
        r"don't miss",
        r"buy now|sell now",
        r"leverage|stop loss|take profit",
    )
]


def _text(lang: str, key: str, **values: object) -> str:
    return FEED_TEXTS[lang][key].format(**values)


def _template_regex(template: str) -> re.Pattern[str]:
    out, pos = "", 0
    for m in re.finditer(r"\{(\w+)\}", template):
        out += re.escape(template[pos : m.start()]) + rf"(?P<{m.group(1)}>\d+(?:\.\d+)?)"
        pos = m.end()
    return re.compile(out + re.escape(template[pos:]) + r"\Z")


_ES_PATTERNS = [(key, _template_regex(t)) for key, t in FEED_TEXTS["es"].items()]


def localize_text(text: str, lang: str) -> str:
    """A stored (Spanish) feed sentence in `lang`. Text that is not one of the
    feed's own sentences is returned unchanged rather than guessed."""
    if lang == "es" or lang not in FEED_TEXTS:
        return text
    for key, pattern in _ES_PATTERNS:
        m = pattern.match(text)
        if m:
            return _text(lang, key, **m.groupdict())
    return text


def compute_reading(coin: str, klines: list[Kline], now: datetime) -> dict:
    """Pure: candles → one feed entry (no id, no time of insert)."""
    if len(klines) < 60:
        raise ValueError(f"{coin}: not enough candles ({len(klines)})")
    pair = f"{coin}USDT"
    snap = MarketSnapshot(
        pair=pair,
        exchange="bitunix",
        taken_at=now,
        klines=klines,
        mark_price=klines[-1].close,
    )
    votes: dict[str, list[str]] = {"buy": [], "sell": []}
    for key in CATALOG:
        strat, params = build(key)
        sig = strat.evaluate(snap, params, StrategyContext(now=now))
        if sig is None:
            continue
        votes["buy" if sig.side == Side.LONG else "sell"].append(key)

    rsi_v = next((v for v in reversed(rsi(klines, 14)) if v is not None), None)
    adx_v = next((v for v in reversed(adx(klines, 14)) if v is not None), None)
    back = klines[-25] if len(klines) >= 25 else klines[0]
    change = (klines[-1].close - back.close) / back.close * 100

    if votes["buy"] and not votes["sell"]:
        state = "buy"
    elif votes["sell"] and not votes["buy"]:
        state = "sell"
    else:
        state = "wait"
    agree = len(votes[state]) if state != "wait" else 0
    strong = adx_v is not None and adx_v >= 25
    confidence = "none" if state == "wait" else ("high" if agree >= 2 or strong else "medium")

    short = _text("es", f"short_{state}")
    if abs(change) < Decimal("0.2"):
        b1 = _text("es", "change_flat")
    else:
        b1 = _text("es", "change_up" if change > 0 else "change_down", pct=_pct(change))
    if adx_v is None:
        b2 = _text("es", "adx_none")
    else:
        level = "high" if adx_v >= 25 else ("moderate" if adx_v >= 18 else "low")
        b2 = _text("es", f"adx_{level}", adx=f"{adx_v:.0f}")
    if rsi_v is None:
        b3 = _text("es", "rsi_none")
    elif rsi_v >= 70:
        b3 = _text("es", "rsi_high", rsi=f"{rsi_v:.0f}")
    elif rsi_v <= 30:
        b3 = _text("es", "rsi_low", rsi=f"{rsi_v:.0f}")
    else:
        b3 = _text("es", "rsi_mid", rsi=f"{rsi_v:.0f}")
    if state == "wait":
        explanation = _text(
            "es", "explain_split" if votes["buy"] and votes["sell"] else "explain_none"
        )
    else:
        explanation = _text("es", "explain_agree", agree=agree, total=len(CATALOG))

    if not text_is_clean(short, b1, b2, b3, explanation):  # pragma: no cover - templates are fixed
        raise ValueError("feed text failed the banned-phrase check")
    return {
        "coin": coin,
        "pair": pair,
        "interval": FEED_INTERVAL,
        "state": state,
        "confidence": confidence,
        "why_short": short,
        "why_bullets": [b1, b2, b3],
        "explanation": explanation,
        "strategies": sorted(votes[state]) if state != "wait" else [],
        "ref_price_usd": klines[-1].close,
    }


async def run_feed_tick(
    store: FeedStore,
    source: KlineSource,
    *,
    usd_mxn: Decimal | None,
    now: datetime | None = None,
    coins: tuple[str, ...] = FEED_COINS,
) -> dict[str, str]:
    """One pass over every coin. Returns coin → 'inserted' | 'unchanged' | 'error'."""
    ts = now or datetime.now(UTC)
    out: dict[str, str] = {}
    for coin in coins:
        try:
            klines = await source.klines(f"{coin}USDT", FEED_INTERVAL, limit=FEED_BARS)
            klines = sorted(klines, key=lambda k: k.open_time)
            reading = compute_reading(coin, klines, ts)
            last = await store.latest_feed_entry(coin=coin)
            fresh = last is not None and ts - _as_dt(last["created_at"]) < REFRESH_AFTER
            if last is not None and last["state"] == reading["state"] and fresh:
                out[coin] = "unchanged"
                continue
            reading["ref_price_mxn"] = (
                (reading["ref_price_usd"] * usd_mxn).quantize(Decimal("0.01"))
                if usd_mxn is not None
                else None
            )
            await store.add_feed_entry(entry=reading)
            out[coin] = "inserted"
        except Exception as e:  # one coin never stops the others
            log.error("feed %s failed: %s", coin, e)
            out[coin] = "error"
    return out


def _as_dt(v) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(v))


def wire(entry: dict, lang: str = "es") -> dict:
    """A stored entry as the hub reads it (no strategy internals beyond names),
    its text in `lang` (stored rows are Spanish)."""
    mxn = entry.get("ref_price_mxn")

    def t(text: str) -> str:
        return localize_text(text, lang)

    return {
        "id": str(entry["id"]),
        "coin": entry["coin"],
        "state": entry["state"],
        "confidence": entry["confidence"],
        "explanation": t(entry["explanation"]),
        "at": _as_dt(entry["created_at"]).isoformat(),
        "ref_price_mxn": float(mxn) if mxn is not None else None,
        "why": {"short": t(entry["why_short"]), "bullets": [t(b) for b in entry["why_bullets"]]},
    }
