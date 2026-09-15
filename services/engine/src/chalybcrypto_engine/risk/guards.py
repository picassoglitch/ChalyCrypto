"""Hard guards (cannot be bypassed in any mode).

Per ARCHITECTURE §4 each guard returns either RejectReason.OK or a specific reason. Guards
do NOT block management of existing positions — they only gate NEW entries. The caller is
responsible for routing rejected entries to the audit log (CLAUDE.md rule 9).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from chalybcrypto_shared import Mode, RiskProfile

from .types import AccountState, RejectReason


_BPS = Decimal("10000")


def check_account_lock(account: AccountState) -> RejectReason:
    """Account-protection lock = global kill on new entries."""
    return (
        RejectReason.ACCOUNT_PROTECTION_LOCK
        if account.account_protection_lock
        else RejectReason.OK
    )


def check_paper_gate(account: AccountState, *, mode: Mode) -> RejectReason:
    """Live modes require the paper gate satisfied. Enforced in code AND DB (CLAUDE.md rule 5)."""
    if mode in (Mode.SEMI_AUTO, Mode.FULL_AUTO) and not account.paper_gate_unlocked:
        return RejectReason.PAPER_GATE_UNMET
    return RejectReason.OK


def check_stale_price(
    account: AccountState,
    *,
    now: datetime,
    max_seconds: int,
) -> RejectReason:
    """Last tick must be fresh. Unknown last_tick → fail safe (reject)."""
    if account.last_tick_at is None:
        return RejectReason.STALE_PRICE
    age = (now - account.last_tick_at).total_seconds()
    if age > max_seconds:
        return RejectReason.STALE_PRICE
    return RejectReason.OK


def check_loss_guards(account: AccountState, profile: RiskProfile) -> RejectReason:
    """Daily / weekly / drawdown limits. Each is checked vs equity in bps.

    These lock NEW entries; managing open positions remains allowed (caller decision).
    """
    if account.peak_equity > 0:
        # Drawdown is on peak_equity, the conservative reference.
        dd_bps = (
            (account.peak_equity - account.equity) / account.peak_equity * _BPS
            if account.equity < account.peak_equity
            else Decimal("0")
        )
        if dd_bps >= profile.max_drawdown_bps:
            return RejectReason.DRAWDOWN_LIMIT

    # Daily / weekly loss are absolute losses vs *peak* equity, normalized to bps.
    if account.peak_equity > 0:
        if account.daily_realized_pnl < 0:
            daily_loss_bps = (-account.daily_realized_pnl) / account.peak_equity * _BPS
            if daily_loss_bps >= profile.max_daily_loss_bps:
                return RejectReason.DAILY_LOSS_LIMIT
        if account.weekly_realized_pnl < 0:
            weekly_loss_bps = (-account.weekly_realized_pnl) / account.peak_equity * _BPS
            if weekly_loss_bps >= profile.max_weekly_loss_bps:
                return RejectReason.WEEKLY_LOSS_LIMIT

    return RejectReason.OK


def check_exposure_caps(account: AccountState, profile: RiskProfile) -> RejectReason:
    """Caps that apply BEFORE sizing increases exposure. Sizing also re-checks itself."""
    if account.open_positions_count >= profile.max_open_positions:
        return RejectReason.MAX_OPEN_POSITIONS
    if account.trades_last_hour >= profile.max_trades_per_hour:
        return RejectReason.MAX_TRADES_PER_HOUR
    return RejectReason.OK


def check_cooldowns(
    account: AccountState,
    profile: RiskProfile,
    *,
    now: datetime,
) -> RejectReason:
    """Cool-off after a loss / volatility spike."""
    if account.last_loss_at is not None:
        delta = (now - account.last_loss_at).total_seconds()
        if delta < profile.cooldown_after_loss_seconds:
            return RejectReason.COOLDOWN_AFTER_LOSS
    if account.last_volatility_spike_at is not None:
        delta = (now - account.last_volatility_spike_at).total_seconds()
        if delta < profile.cooldown_after_volatility_spike_seconds:
            return RejectReason.COOLDOWN_AFTER_VOLATILITY
    return RejectReason.OK
