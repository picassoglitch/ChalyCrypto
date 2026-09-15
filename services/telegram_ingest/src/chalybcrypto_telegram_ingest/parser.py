"""Telegram signal parser — deterministic, pure, no LLM (CLAUDE.md rule 2).

Real signal messages from copy-trading channels are messy: mixed Spanish/English, lots of
emojis, inconsistent ordering, sometimes embedded TPs/SL in tables. This parser uses
regex + simple heuristics to extract the structured fields. It refuses to guess: any
required field that's missing → return None and let downstream label the message
'unparseable' (audit log row, not a trade).

Parsed signals are CANDIDATES — they enter the same pipeline as scanner signals and
must pass strategy validation + EV + risk gates (ARCHITECTURE §3, CLAUDE.md note).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from pydantic import BaseModel, ConfigDict

from chalybcrypto_shared import MarginType, Side, dedup_hash


_FROZEN = ConfigDict(extra="forbid", frozen=True)


class ParsedTelegramSignal(BaseModel):
    model_config = _FROZEN

    pair: str
    side: Side
    entry: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profits: list[Decimal] = []
    leverage: Decimal | None = None
    timeframe: str | None = None
    margin_type: MarginType | None = None
    raw_text: str
    dedup_hash: str


_PAIR_RE = re.compile(
    r"\b([A-Z]{2,10})\s*[/_-]?\s*(USDT|USD|BUSD|USDC)\b",
    re.IGNORECASE,
)

# Side markers in Spanish + English. Direction wins over verb so "COMPRA LARGA" -> LONG.
_LONG_PATTERNS = (
    r"\blong\b",
    r"\bbuy\b",
    r"\bcompra(r)?\b",
    r"\blarg[oa]\b",
)
_SHORT_PATTERNS = (
    r"\bshort\b",
    r"\bsell\b",
    r"\bvend(er|e)?\b",
    r"\bcort[oa]\b",
)


def _first_match(patterns: Iterable[str], text: str) -> bool:
    return any(re.search(p, text, flags=re.IGNORECASE) for p in patterns)


def _grab_number(label_patterns: Iterable[str], text: str) -> Decimal | None:
    """Find a number on the same logical line as any of `label_patterns`."""
    for p in label_patterns:
        m = re.search(rf"{p}[^\d-]*([\d]+(?:[.,]\d+)?)", text, flags=re.IGNORECASE)
        if m:
            return Decimal(m.group(1).replace(",", "."))
    return None


def _grab_all_numbers(label_patterns: Iterable[str], text: str) -> list[Decimal]:
    """Find all numbers tagged with one of `label_patterns` (e.g. TP1/TP2/TP3)."""
    out: list[Decimal] = []
    for p in label_patterns:
        for m in re.finditer(
            rf"{p}\d*[^\d-]*([\d]+(?:[.,]\d+)?)", text, flags=re.IGNORECASE
        ):
            out.append(Decimal(m.group(1).replace(",", ".")))
    return out


def _grab_leverage(text: str) -> Decimal | None:
    m = re.search(r"(?:leverage|apalancamiento|lev)[^\d]*([\d]+(?:[.,]\d+)?)\s*[xX]?",
                  text, flags=re.IGNORECASE)
    if m:
        return Decimal(m.group(1).replace(",", "."))
    m = re.search(r"(\d+)x\b", text, flags=re.IGNORECASE)
    if m:
        return Decimal(m.group(1))
    return None


def _grab_timeframe(text: str) -> str | None:
    m = re.search(r"\b(\d+)\s*(m|min|h|hr|hour|d|day)\b", text, flags=re.IGNORECASE)
    if not m:
        return None
    n, unit = m.group(1), m.group(2).lower()
    if unit.startswith("h"):
        return f"{n}h"
    if unit.startswith("d"):
        return f"{n}d"
    return f"{n}m"


_ENTRY_AFTER_SIDE_RE = re.compile(
    r"\b(?:long|short|buy|sell|compra(?:r)?|vend(?:er|e)?)\b"
    r"(?:[^\d-]+?(?:en|at|a)\b)?"
    r"[^\d-]{1,40}?([\d]+(?:[.,]\d+)?)",
    re.IGNORECASE,
)


def _grab_entry_after_side_keyword(text: str) -> Decimal | None:
    """Fallback: if no labeled entry, take the first number that follows a side keyword
    (optionally separated by 'en'/'a'/'at'). Catches Spanish 'COMPRA en 3500' and
    'VENDER ... a 150'.
    """
    m = _ENTRY_AFTER_SIDE_RE.search(text)
    if m:
        return Decimal(m.group(1).replace(",", "."))
    return None


def _grab_margin_type(text: str) -> MarginType | None:
    if re.search(r"\bcross\b", text, flags=re.IGNORECASE):
        return MarginType.CROSS
    if re.search(r"\bisolated\b|\baislad[oa]\b", text, flags=re.IGNORECASE):
        return MarginType.ISOLATED
    return None


def parse_signal(text: str, *, now: datetime | None = None) -> ParsedTelegramSignal | None:
    """Return a structured signal or None when the message is unparseable.

    Required: pair + side. Everything else is opportunistic; downstream (the risk engine
    and EV gate) decides whether the missing fields are fatal.
    """
    if not text or not text.strip():
        return None

    # Pair
    pm = _PAIR_RE.search(text)
    if not pm:
        return None
    pair = f"{pm.group(1).upper()}{pm.group(2).upper()}"

    # Side
    has_long = _first_match(_LONG_PATTERNS, text)
    has_short = _first_match(_SHORT_PATTERNS, text)
    if has_long and not has_short:
        side = Side.LONG
    elif has_short and not has_long:
        side = Side.SHORT
    else:
        return None  # ambiguous or absent

    entry = _grab_number(
        (r"entry", r"entrada", r"precio\s*de\s*entrada", r"buy[\s_-]*price", r"\bopen\b"), text,
    )
    # Spanish fallback: "COMPRA en 3500" / "VENDER a 150" / "LONG at 60000".
    if entry is None:
        entry = _grab_entry_after_side_keyword(text)
    sl = _grab_number(
        (r"\bsl\b", r"stop[\s_-]*loss", r"stop[\s_-]*p[ée]rdida", r"stop"), text,
    )
    tps = _grab_all_numbers((r"\btp", r"take[\s_-]*profit", r"objetivo", r"target"), text)
    leverage = _grab_leverage(text)
    timeframe = _grab_timeframe(text)
    margin_type = _grab_margin_type(text)

    stamp = (now or datetime.now(timezone.utc)).isoformat()
    h = dedup_hash("telegram", pair, side.value, entry if entry is not None else "", stamp)

    return ParsedTelegramSignal(
        pair=pair,
        side=side,
        entry=entry,
        stop_loss=sl,
        take_profits=tps,
        leverage=leverage,
        timeframe=timeframe,
        margin_type=margin_type,
        raw_text=text,
        dedup_hash=h,
    )
