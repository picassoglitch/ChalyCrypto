"""Señales shared feed: one general reading per coin, the same for everyone.

What the Chalyb hub shows in /app/senales (REVISION-LEGAL C1 in the hub):
  * computed from MARKET DATA ONLY — the engine's strategies on Bitunix 1h
    candles. No account, balance, holding, risk profile or user anywhere.
  * no trade setup: no entry, stop, take-profit or leverage leaves this module.
    Only the direction the strategies agree on (buy / sell / wait), how many
    agree, and a plain-Spanish "por qué" about the PRICE.
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

    short = {
        "buy": "Las lecturas del precio en las últimas horas apuntan hacia arriba.",
        "sell": "Las lecturas del precio en las últimas horas apuntan hacia abajo.",
        "wait": "No hay una dirección clara por ahora. Te avisamos cuando la haya.",
    }[state]
    if abs(change) < Decimal("0.2"):
        b1 = "En las últimas 24 horas casi no se movió."
    else:
        b1 = f"En las últimas 24 horas {'subió' if change > 0 else 'bajó'} {_pct(change)}%."
    if adx_v is None:
        b2 = "Todavía no hay datos para medir la fuerza de la tendencia."
    else:
        fuerza = "alta" if adx_v >= 25 else ("moderada" if adx_v >= 18 else "baja")
        b2 = f"La fuerza de la tendencia (ADX) está en {adx_v:.0f}: es {fuerza}."
    if rsi_v is None:
        b3 = "Todavía no hay datos para el RSI."
    elif rsi_v >= 70:
        b3 = f"El RSI está en {rsi_v:.0f}: subió rápido en poco tiempo."
    elif rsi_v <= 30:
        b3 = f"El RSI está en {rsi_v:.0f}: bajó rápido en poco tiempo."
    else:
        b3 = f"El RSI está en {rsi_v:.0f}: en una zona media."
    if state == "wait":
        explanation = (
            "Las estrategias de Señales no coinciden en una dirección."
            if votes["buy"] and votes["sell"]
            else "Ninguna estrategia de Señales ve una dirección clara."
        )
    else:
        explanation = f"{agree} de {len(CATALOG)} estrategias de Señales coinciden en esta lectura."

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


def wire(entry: dict) -> dict:
    """A stored entry as the hub reads it (no strategy internals beyond names)."""
    mxn = entry.get("ref_price_mxn")
    return {
        "id": str(entry["id"]),
        "coin": entry["coin"],
        "state": entry["state"],
        "confidence": entry["confidence"],
        "explanation": entry["explanation"],
        "at": _as_dt(entry["created_at"]).isoformat(),
        "ref_price_mxn": float(mxn) if mxn is not None else None,
        "why": {"short": entry["why_short"], "bullets": list(entry["why_bullets"])},
    }
