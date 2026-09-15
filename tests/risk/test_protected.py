"""Protected-profit stop (ARCHITECTURE §6).

Invariants under test:
  * Floor ratchets up on new peak.
  * Floor never decreases on a pullback.
  * Trigger fires when net_pnl <= floor.
  * Floor is computed on NET PnL (after exit fee + accrued funding).
  * Spec example: up $100, continue, giveback 0.30 → protect at least $70.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from chalybcrypto_engine.risk import PositionState
from chalybcrypto_engine.risk.protected import (
    advance_protected_floor,
    breakeven_stop_price,
    gross_unrealized_pnl,
    net_unrealized_pnl,
    should_trigger_protected_exit,
)
from chalybcrypto_shared import MarginType, Side

from ._helpers import make_profile


def _position(side=Side.LONG, entry=Decimal("60000"), qty=Decimal("0.1")) -> PositionState:
    return PositionState(
        pair="BTCUSDT",
        side=side,
        qty=qty,
        entry_price=entry,
        leverage=Decimal("10"),
        margin_type=MarginType.ISOLATED,
    )


def test_spec_example_up_100_protects_at_least_70():
    state = advance_protected_floor(
        current_net_pnl=Decimal("100"),
        prior_peak_net=Decimal("0"),
        prior_floor_net=None,
        giveback=Decimal("0.30"),
    )
    assert state.peak_gain_net == Decimal("100")
    assert state.protected_floor_net == Decimal("70")


def test_floor_ratchets_up_on_new_peak():
    s1 = advance_protected_floor(
        current_net_pnl=Decimal("100"),
        prior_peak_net=Decimal("0"),
        prior_floor_net=None,
        giveback=Decimal("0.30"),
    )
    s2 = advance_protected_floor(
        current_net_pnl=Decimal("150"),
        prior_peak_net=s1.peak_gain_net,
        prior_floor_net=s1.protected_floor_net,
        giveback=Decimal("0.30"),
    )
    assert s2.peak_gain_net == Decimal("150")
    assert s2.protected_floor_net == Decimal("105")


def test_floor_does_not_decrease_on_pullback():
    s1 = advance_protected_floor(
        current_net_pnl=Decimal("150"),
        prior_peak_net=Decimal("0"),
        prior_floor_net=None,
        giveback=Decimal("0.30"),
    )
    s2 = advance_protected_floor(
        current_net_pnl=Decimal("120"),  # pullback
        prior_peak_net=s1.peak_gain_net,
        prior_floor_net=s1.protected_floor_net,
        giveback=Decimal("0.30"),
    )
    assert s2.peak_gain_net == Decimal("150")  # peak preserved
    assert s2.protected_floor_net == Decimal("105")  # floor preserved


def test_trigger_fires_when_net_touches_floor():
    assert should_trigger_protected_exit(
        current_net_pnl=Decimal("70"), protected_floor_net=Decimal("70")
    ) is True
    assert should_trigger_protected_exit(
        current_net_pnl=Decimal("69"), protected_floor_net=Decimal("70")
    ) is True
    assert should_trigger_protected_exit(
        current_net_pnl=Decimal("71"), protected_floor_net=Decimal("70")
    ) is False


def test_trigger_skips_when_floor_zero_or_negative():
    """Don't lock in a loss just because a giveback was scheduled."""
    assert should_trigger_protected_exit(
        current_net_pnl=Decimal("-10"), protected_floor_net=Decimal("0")
    ) is False
    assert should_trigger_protected_exit(
        current_net_pnl=Decimal("-10"), protected_floor_net=Decimal("-5")
    ) is False


def test_floor_is_net_of_exit_fee_and_funding():
    pos = _position(qty=Decimal("0.1"), entry=Decimal("60000"))
    # mark moved up to 61000; gross = 0.1 * 1000 = 100.
    # exit fee at 6 bps on 0.1 * 61000 = 6100 notional → 3.66.
    # accrued funding 0.5 → net = 100 - 3.66 - 0.5 = 95.84.
    net = net_unrealized_pnl(
        pos, Decimal("61000"), exit_taker_bps=Decimal("6"), accrued_funding=Decimal("0.5")
    )
    assert net == Decimal("100") - Decimal("3.66") - Decimal("0.5")


def test_short_position_pnl_inverts_direction():
    pos = _position(side=Side.SHORT, entry=Decimal("60000"), qty=Decimal("0.1"))
    assert gross_unrealized_pnl(pos, Decimal("59000")) == Decimal("100")
    assert gross_unrealized_pnl(pos, Decimal("61000")) == Decimal("-100")


def test_breakeven_stop_includes_fee_buffer():
    pos = _position(entry=Decimal("60000"))
    res = breakeven_stop_price(position=pos, profile=make_profile(), exit_taker_bps=Decimal("6"))
    # 60000 * 6/10000 = 36 → stop 60036 for long
    assert res.stop_price == Decimal("60036")


def test_breakeven_short_subtracts_buffer():
    pos = _position(side=Side.SHORT, entry=Decimal("60000"))
    res = breakeven_stop_price(position=pos, profile=make_profile(), exit_taker_bps=Decimal("6"))
    assert res.stop_price == Decimal("59964")


def test_invalid_giveback_raises():
    with pytest.raises(ValueError):
        advance_protected_floor(
            current_net_pnl=Decimal("100"),
            prior_peak_net=Decimal("0"),
            prior_floor_net=None,
            giveback=Decimal("0"),
        )
    with pytest.raises(ValueError):
        advance_protected_floor(
            current_net_pnl=Decimal("100"),
            prior_peak_net=Decimal("0"),
            prior_floor_net=None,
            giveback=Decimal("1.5"),
        )


def test_breakeven_with_zero_fee_buffer_lands_at_entry():
    pos = _position()
    res = breakeven_stop_price(position=pos, profile=make_profile(), exit_taker_bps=Decimal("0"))
    assert res.stop_price == Decimal("60000")
