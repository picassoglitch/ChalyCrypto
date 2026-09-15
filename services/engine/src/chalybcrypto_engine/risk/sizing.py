"""Position sizing.

Risk-based: the size is whatever loses exactly `max_risk_per_trade_bps` of equity if the
stop fires at SL — fees + funding included separately in the EV gate, not double-counted
here. Distance-from-entry-to-SL bounds the qty; max_leverage and exposure caps bound it
from above. If both ends squeeze it below an exchange's minimum (or to zero), reject.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from chalybcrypto_shared import RiskProfile, Side, Signal

from .types import AccountState, RejectReason


_FROZEN = ConfigDict(extra="forbid", frozen=True)
_BPS = Decimal("10000")


class SizeIntent(BaseModel):
    model_config = _FROZEN

    qty: Decimal
    notional: Decimal
    leverage: Decimal
    margin_required: Decimal
    reject: RejectReason = RejectReason.OK

    @property
    def approved(self) -> bool:
        return self.reject == RejectReason.OK


def size_position(
    signal: Signal,
    account: AccountState,
    risk_profile: RiskProfile,
    *,
    min_qty: Decimal = Decimal("0"),
) -> SizeIntent:
    """Decide qty + leverage + margin. Returns SizeIntent.approved=False with a reason on reject."""

    stop_dist = abs(signal.entry - signal.stop_loss)
    if stop_dist <= 0:
        return _reject(RejectReason.INVALID_SIGNAL)

    if account.equity <= 0:
        return _reject(RejectReason.ACCOUNT_PROTECTION_LOCK)

    risk_amount = account.equity * (risk_profile.max_risk_per_trade_bps / _BPS)
    raw_qty = risk_amount / stop_dist

    # Min-RR: each TP at least min_rr * stop_dist away in the favorable direction.
    if signal.take_profits:
        nearest_tp = signal.take_profits[0]
        if signal.side == Side.LONG:
            tp_dist = nearest_tp - signal.entry
        else:
            tp_dist = signal.entry - nearest_tp
        if tp_dist <= 0 or tp_dist / stop_dist < risk_profile.min_rr:
            return _reject(RejectReason.MIN_RR_NOT_MET)

    # Honor signal's requested leverage but clamp to risk_profile cap.
    lev = min(signal.leverage, risk_profile.max_leverage)
    if lev <= 0:
        return _reject(RejectReason.INVALID_SIGNAL)

    notional = raw_qty * signal.entry
    margin = notional / lev if lev > 0 else notional

    # Exposure caps — measured as margin tied up per asset / total, expressed in bps of
    # equity. This is leverage-agnostic: "30% per asset" means "no single asset uses more
    # than 30% of equity as margin" regardless of how leveraged that margin is.
    asset_margin_bps = (margin / account.equity) * _BPS if account.equity > 0 else _BPS
    if asset_margin_bps > risk_profile.max_exposure_per_asset_bps:
        return _reject(RejectReason.MAX_EXPOSURE_PER_ASSET)

    projected_total_bps = account.total_exposure_bps + asset_margin_bps
    if projected_total_bps > risk_profile.max_total_exposure_bps:
        return _reject(RejectReason.MAX_TOTAL_EXPOSURE)

    if raw_qty < min_qty:
        return _reject(RejectReason.SIZE_BELOW_MIN)

    if raw_qty <= 0:
        return _reject(RejectReason.SIZE_RISK_UNREACHABLE)

    return SizeIntent(qty=raw_qty, notional=notional, leverage=lev, margin_required=margin)


def _reject(reason: RejectReason) -> SizeIntent:
    return SizeIntent(
        qty=Decimal("0"),
        notional=Decimal("0"),
        leverage=Decimal("0"),
        margin_required=Decimal("0"),
        reject=reason,
    )
