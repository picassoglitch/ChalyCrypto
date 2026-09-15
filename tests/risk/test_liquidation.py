from __future__ import annotations

from decimal import Decimal

from chalybcrypto_engine.risk.liquidation import (
    liquidation_distance_bps,
    liquidation_price,
    passes_min_distance,
)
from chalybcrypto_shared import Side


def test_long_liq_below_entry_by_approximately_1_over_lev():
    # Long at 60k, 10x, mm=0.5% → liq ≈ 60k * (1 - 0.1 + 0.005) = 60k * 0.905 = 54300
    liq = liquidation_price(side=Side.LONG, entry=Decimal("60000"), leverage=Decimal("10"))
    assert liq == Decimal("60000") * Decimal("0.905")


def test_short_liq_above_entry():
    liq = liquidation_price(side=Side.SHORT, entry=Decimal("60000"), leverage=Decimal("10"))
    assert liq == Decimal("60000") * Decimal("1.095")


def test_higher_leverage_brings_liq_closer():
    near = liquidation_price(side=Side.LONG, entry=Decimal("60000"), leverage=Decimal("50"))
    far = liquidation_price(side=Side.LONG, entry=Decimal("60000"), leverage=Decimal("5"))
    assert near > far  # closer to entry => higher price for a long


def test_distance_bps_is_positive_and_correct():
    dist = liquidation_distance_bps(
        side=Side.LONG, entry=Decimal("60000"), liq=Decimal("54300")
    )
    # 5700/60000 * 10000 = 950
    assert dist == Decimal("950")


def test_passes_min_distance_rejects_tight_50x_under_500bps_min():
    # 50x → distance ~1.5% = 150 bps. Min 500 bps → reject.
    ok, liq, dist = passes_min_distance(
        side=Side.LONG,
        entry=Decimal("60000"),
        leverage=Decimal("50"),
        min_distance_bps=Decimal("500"),
    )
    # 60000 * (1 - 1/50 + 0.005) = 60000 * 0.985 = 59100; 900/60000*10000 = 150 bps
    assert ok is False
    assert dist == Decimal("150")


def test_zero_leverage_fails_safe():
    ok, liq, dist = passes_min_distance(
        side=Side.LONG,
        entry=Decimal("60000"),
        leverage=Decimal("0"),
        min_distance_bps=Decimal("100"),
    )
    assert ok is False
    assert liq is None
    assert dist is None
