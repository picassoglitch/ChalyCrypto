from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from chalybcrypto_engine.risk import RejectReason
from chalybcrypto_engine.risk.guards import (
    check_account_lock,
    check_cooldowns,
    check_exposure_caps,
    check_loss_guards,
    check_paper_gate,
    check_stale_price,
)
from chalybcrypto_shared import Mode

from ._helpers import NOW, make_account, make_profile


def test_account_lock_blocks_new_entries():
    locked = make_account(locked=True)
    assert check_account_lock(locked) == RejectReason.ACCOUNT_PROTECTION_LOCK
    assert check_account_lock(make_account()) == RejectReason.OK


def test_paper_gate_blocks_semi_auto_until_unlocked():
    pending = make_account(paper_gate_unlocked=False)
    assert check_paper_gate(pending, mode=Mode.SEMI_AUTO) == RejectReason.PAPER_GATE_UNMET
    # Paper mode itself is always allowed.
    assert check_paper_gate(pending, mode=Mode.PAPER) == RejectReason.OK


def test_stale_price_rejects_unknown_tick():
    no_tick = make_account(last_tick_at=None)
    assert check_stale_price(no_tick, now=NOW, max_seconds=5) == RejectReason.STALE_PRICE


def test_stale_price_rejects_old_tick():
    stale = make_account(last_tick_at=NOW - timedelta(seconds=10))
    assert check_stale_price(stale, now=NOW, max_seconds=5) == RejectReason.STALE_PRICE
    fresh = make_account(last_tick_at=NOW - timedelta(seconds=2))
    assert check_stale_price(fresh, now=NOW, max_seconds=5) == RejectReason.OK


def test_daily_loss_limit_rejects_when_reached():
    # peak=10000, daily pnl=-300 → 300 bps loss; profile cap 300 → reject.
    acct = make_account(
        equity=Decimal("9700"),
        peak_equity=Decimal("10000"),
        daily_pnl=Decimal("-300"),
    )
    prof = make_profile(max_daily_loss_bps=Decimal("300"), max_drawdown_bps=Decimal("10000"))
    assert check_loss_guards(acct, prof) == RejectReason.DAILY_LOSS_LIMIT


def test_weekly_loss_limit_rejects():
    acct = make_account(
        equity=Decimal("9200"),
        peak_equity=Decimal("10000"),
        weekly_pnl=Decimal("-800"),
    )
    prof = make_profile(max_weekly_loss_bps=Decimal("800"), max_drawdown_bps=Decimal("10000"))
    assert check_loss_guards(acct, prof) == RejectReason.WEEKLY_LOSS_LIMIT


def test_drawdown_limit_rejects():
    acct = make_account(equity=Decimal("8500"), peak_equity=Decimal("10000"))
    prof = make_profile(max_drawdown_bps=Decimal("1500"))
    assert check_loss_guards(acct, prof) == RejectReason.DRAWDOWN_LIMIT


def test_max_open_positions_rejects():
    acct = make_account(open_positions=3)
    prof = make_profile(max_open_positions=3)
    assert check_exposure_caps(acct, prof) == RejectReason.MAX_OPEN_POSITIONS


def test_max_trades_per_hour_rejects():
    acct = make_account(trades_last_hour=6)
    prof = make_profile(max_trades_per_hour=6)
    assert check_exposure_caps(acct, prof) == RejectReason.MAX_TRADES_PER_HOUR


def test_cooldown_after_loss_blocks_during_window():
    acct = make_account(last_loss_at=NOW - timedelta(seconds=300))  # 5 min ago
    prof = make_profile(cooldown_loss_seconds=900)  # 15 min window
    assert check_cooldowns(acct, prof, now=NOW) == RejectReason.COOLDOWN_AFTER_LOSS


def test_cooldown_after_loss_releases_after_window():
    acct = make_account(last_loss_at=NOW - timedelta(seconds=901))
    prof = make_profile(cooldown_loss_seconds=900)
    assert check_cooldowns(acct, prof, now=NOW) == RejectReason.OK


def test_cooldown_after_volatility_blocks():
    acct = make_account(last_volatility_at=NOW - timedelta(seconds=100))
    prof = make_profile(cooldown_vol_seconds=300)
    assert check_cooldowns(acct, prof, now=NOW) == RejectReason.COOLDOWN_AFTER_VOLATILITY


def test_loss_guards_pass_when_within_limits():
    acct = make_account(daily_pnl=Decimal("-50"), weekly_pnl=Decimal("-100"))
    prof = make_profile()
    assert check_loss_guards(acct, prof) == RejectReason.OK
