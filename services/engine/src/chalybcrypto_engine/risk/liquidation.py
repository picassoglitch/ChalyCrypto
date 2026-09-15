"""Liquidation price estimator + min-distance gate.

Conservative isolated-margin formula:
  long  → liq = entry * (1 - (1/lev) + mm_rate)
  short → liq = entry * (1 + (1/lev) - mm_rate)

This understates distance vs cross-margin (which can also draw on free balance), so
it's the safe choice: cross-margin trades surviving this check survive in reality;
isolated-margin trades passing this also pass.

ARCHITECTURE §4: stale / unknown inputs → fail safe (reject).
"""

from __future__ import annotations

from decimal import Decimal

from chalybcrypto_shared import Side


# Bitunix MM rate ~ 0.5% at the 50x bracket; configurable per-call so other venues fit.
DEFAULT_MAINTENANCE_MARGIN_RATE = Decimal("0.005")
_BPS = Decimal("10000")


def liquidation_price(
    *,
    side: Side,
    entry: Decimal,
    leverage: Decimal,
    maintenance_margin_rate: Decimal = DEFAULT_MAINTENANCE_MARGIN_RATE,
) -> Decimal | None:
    """Return estimated isolated-margin liquidation price. None on unknown input."""
    if entry <= 0 or leverage <= 0 or maintenance_margin_rate < 0:
        return None
    inv_lev = Decimal("1") / leverage
    if side == Side.LONG:
        return entry * (Decimal("1") - inv_lev + maintenance_margin_rate)
    return entry * (Decimal("1") + inv_lev - maintenance_margin_rate)


def liquidation_distance_bps(*, side: Side, entry: Decimal, liq: Decimal | None) -> Decimal | None:
    """Distance from entry to liq in bps of entry, positive. None if unknown."""
    if liq is None or entry <= 0:
        return None
    raw = abs(entry - liq) / entry
    return raw * _BPS


def passes_min_distance(
    *,
    side: Side,
    entry: Decimal,
    leverage: Decimal,
    min_distance_bps: Decimal,
    maintenance_margin_rate: Decimal = DEFAULT_MAINTENANCE_MARGIN_RATE,
) -> tuple[bool, Decimal | None, Decimal | None]:
    """Convenience: returns (passes, liq, distance_bps). passes=False on any unknown input."""
    liq = liquidation_price(
        side=side, entry=entry, leverage=leverage, maintenance_margin_rate=maintenance_margin_rate
    )
    dist = liquidation_distance_bps(side=side, entry=entry, liq=liq)
    if liq is None or dist is None:
        return (False, liq, dist)
    return (dist >= min_distance_bps, liq, dist)
