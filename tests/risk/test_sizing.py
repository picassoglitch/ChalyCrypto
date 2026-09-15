from __future__ import annotations

from decimal import Decimal

from chalybcrypto_engine.risk import RejectReason
from chalybcrypto_engine.risk.sizing import size_position

from ._helpers import make_account, make_profile, make_signal


def test_size_matches_risk_amount_over_stop_distance():
    signal = make_signal(entry=Decimal("60000"), stop_loss=Decimal("59700"))  # 300 distance
    account = make_account(equity=Decimal("10000"))
    profile = make_profile(max_risk_bps=Decimal("50"))  # 50bp of $10k = $50

    intent = size_position(signal, account, profile)

    # qty = $50 / 300 = 0.1666...
    assert intent.approved
    assert intent.qty == Decimal("0.1666666666666666666666666667")
    assert intent.notional == intent.qty * Decimal("60000")
    assert intent.leverage == Decimal("10")
    assert intent.margin_required == intent.notional / Decimal("10")


def test_zero_stop_distance_rejected_as_invalid_signal():
    signal = make_signal(entry=Decimal("60000"), stop_loss=Decimal("60000"))
    intent = size_position(signal, make_account(), make_profile())
    assert intent.reject == RejectReason.INVALID_SIGNAL


def test_leverage_clamped_to_profile_max():
    signal = make_signal(leverage=Decimal("100"))
    profile = make_profile(max_leverage=Decimal("20"))
    intent = size_position(signal, make_account(), profile)
    assert intent.approved
    assert intent.leverage == Decimal("20")


def test_min_rr_violation_rejected():
    # 300 stop distance, TP only 200 away → RR ~0.66, below min 1.5
    signal = make_signal(
        entry=Decimal("60000"),
        stop_loss=Decimal("59700"),
        take_profits=[Decimal("60200")],
    )
    intent = size_position(signal, make_account(), make_profile(min_rr=Decimal("1.5")))
    assert intent.reject == RejectReason.MIN_RR_NOT_MET


def test_exposure_per_asset_cap_rejected():
    # Very wide stop → huge qty → notional > per-asset cap.
    signal = make_signal(entry=Decimal("60000"), stop_loss=Decimal("59999"))
    profile = make_profile(
        max_risk_bps=Decimal("200"),
        max_per_asset_bps=Decimal("50"),  # ridiculously low cap
    )
    intent = size_position(signal, make_account(), profile)
    assert intent.reject == RejectReason.MAX_EXPOSURE_PER_ASSET


def test_total_exposure_cap_rejected():
    signal = make_signal()
    account = make_account(total_exposure_bps=Decimal("7990"))
    profile = make_profile(max_total_bps=Decimal("8000"))
    intent = size_position(signal, account, profile)
    assert intent.reject == RejectReason.MAX_TOTAL_EXPOSURE


def test_qty_below_exchange_min_rejected():
    signal = make_signal()
    intent = size_position(signal, make_account(), make_profile(), min_qty=Decimal("999"))
    assert intent.reject == RejectReason.SIZE_BELOW_MIN
