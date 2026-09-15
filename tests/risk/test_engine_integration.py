"""End-to-end RiskEngine.authorize_new_entry — every gate hit, exactly one reason returned.

These are the Phase 3 acceptance tests promised in BUILD_PLAN.md.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from chalybcrypto_engine.risk import (
    InMemoryIdempotencyStore,
    RejectReason,
    RiskEngine,
)
from chalybcrypto_shared import Mode

from ._helpers import (
    NOW,
    make_account,
    make_ev_inputs,
    make_profile,
    make_signal,
    make_stats,
)


_DEFAULT = object()


async def _decide(*, signal=None, account=None, profile=None, ev_inputs=None,
                  stats=_DEFAULT, store=None, mode=Mode.SEMI_AUTO, now=NOW):
    """`stats=_DEFAULT` substitutes defaults; pass `stats=None` to test unknown stats."""
    if stats is _DEFAULT:
        stats = make_stats(
            sample_size=100, win_rate=Decimal("0.55"),
            avg_win_bps=Decimal("80"), avg_loss_bps=Decimal("40"),
        )
    eng = RiskEngine()
    return await eng.authorize_new_entry(
        signal=signal or make_signal(),
        account=account or make_account(),
        risk_profile=profile or make_profile(),
        ev_inputs=ev_inputs or make_ev_inputs(
            taker_bps=Decimal("2"), spread_bps=Decimal("0"), slippage_bps=Decimal("0")
        ),
        strategy_stats=stats,
        idempotency_store=store or InMemoryIdempotencyStore(),
        mode=mode,
        now=now,
    )


# ── happy path ─────────────────────────────────────────────────────────────


async def test_happy_path_approves_with_full_decision_record():
    d = await _decide()
    assert d.approved is True
    assert d.reason == RejectReason.OK.value
    assert d.intended_qty is not None and d.intended_qty > 0
    assert d.intended_leverage is not None
    assert d.ev_net_bps is not None and d.ev_net_bps > 0
    assert d.liquidation_price is not None
    assert d.liquidation_distance_bps is not None
    assert d.fees_round_trip_bps is not None
    assert d.idempotency_key  # carries through for the order-placement step


# ── ordering of fatal gates ────────────────────────────────────────────────


async def test_account_lock_is_checked_first():
    d = await _decide(account=make_account(locked=True))
    assert d.approved is False
    assert d.reason == RejectReason.ACCOUNT_PROTECTION_LOCK.value


async def test_paper_gate_blocks_live_until_unlocked():
    d = await _decide(account=make_account(paper_gate_unlocked=False))
    assert d.approved is False
    assert d.reason == RejectReason.PAPER_GATE_UNMET.value


async def test_paper_mode_bypasses_paper_gate():
    d = await _decide(account=make_account(paper_gate_unlocked=False), mode=Mode.PAPER, stats=None)
    assert d.approved is True


async def test_stale_price_rejects_when_tick_unknown():
    d = await _decide(account=make_account(last_tick_at=None))
    assert d.approved is False
    assert d.reason == RejectReason.STALE_PRICE.value


async def test_stale_price_rejects_old_tick():
    d = await _decide(account=make_account(last_tick_at=NOW - timedelta(seconds=10)))
    assert d.reason == RejectReason.STALE_PRICE.value


# ── loss-side guards ───────────────────────────────────────────────────────


async def test_daily_loss_blocks_new_entries_but_does_not_close_open():
    """Acceptance: daily-loss lock blocks new entries but allows managing open ones.

    Managing open positions never goes through authorize_new_entry — by design, this method
    only authorizes ENTRIES. The test asserts the entry-side reject is clean and stable.
    """
    acct = make_account(
        equity=Decimal("9700"), peak_equity=Decimal("10000"),
        daily_pnl=Decimal("-300"),
    )
    d = await _decide(account=acct, profile=make_profile(max_daily_loss_bps=Decimal("300")))
    assert d.approved is False
    assert d.reason == RejectReason.DAILY_LOSS_LIMIT.value


async def test_drawdown_limit_rejects():
    acct = make_account(equity=Decimal("8400"), peak_equity=Decimal("10000"))
    d = await _decide(account=acct, profile=make_profile(max_drawdown_bps=Decimal("1500")))
    assert d.reason == RejectReason.DRAWDOWN_LIMIT.value


async def test_cooldown_after_loss_rejects():
    acct = make_account(last_loss_at=NOW - timedelta(seconds=60))
    d = await _decide(account=acct, profile=make_profile(cooldown_loss_seconds=900))
    assert d.reason == RejectReason.COOLDOWN_AFTER_LOSS.value


# ── liquidation distance ───────────────────────────────────────────────────


async def test_liquidation_too_close_at_high_leverage():
    sig = make_signal(leverage=Decimal("50"))
    d = await _decide(
        signal=sig,
        profile=make_profile(max_leverage=Decimal("50"), min_liquidation_distance_bps=Decimal("500")),
    )
    assert d.reason == RejectReason.LIQUIDATION_TOO_CLOSE.value
    assert d.liquidation_price is not None


# ── EV gate ────────────────────────────────────────────────────────────────


async def test_unknown_stats_rejected_for_live():
    d = await _decide(stats=None, mode=Mode.SEMI_AUTO)
    assert d.reason == RejectReason.EV_STATS_UNKNOWN.value


async def test_unknown_stats_allowed_for_backtest_and_paper():
    for m in (Mode.BACKTEST, Mode.PAPER):
        d = await _decide(stats=None, mode=m)
        assert d.approved is True, f"unknown stats must be allowed for {m}"


async def test_negative_ev_rejected_for_live():
    losing = make_stats(
        sample_size=100, win_rate=Decimal("0.5"),
        avg_win_bps=Decimal("20"), avg_loss_bps=Decimal("20"),
    )
    d = await _decide(stats=losing, mode=Mode.SEMI_AUTO)
    assert d.reason == RejectReason.EV_NEGATIVE_AFTER_COSTS.value


# ── sizing ─────────────────────────────────────────────────────────────────


async def test_min_rr_rejected_at_engine_level():
    sig = make_signal(
        entry=Decimal("60000"), stop_loss=Decimal("59700"),
        take_profits=[Decimal("60100")],  # RR ~0.33
    )
    d = await _decide(signal=sig)
    assert d.reason == RejectReason.MIN_RR_NOT_MET.value


# ── idempotency ────────────────────────────────────────────────────────────


async def test_same_dedup_hash_rejected_second_time():
    store = InMemoryIdempotencyStore()
    sig = make_signal()
    d1 = await _decide(signal=sig, store=store)
    assert d1.approved is True
    d2 = await _decide(signal=sig, store=store)
    assert d2.reason == RejectReason.DUPLICATE_SIGNAL.value


async def test_distinct_signals_do_not_collide():
    store = InMemoryIdempotencyStore()
    d1 = await _decide(signal=make_signal(entry=Decimal("60000")), store=store)
    d2 = await _decide(signal=make_signal(entry=Decimal("60100")), store=store)
    assert d1.approved is True
    assert d2.approved is True


# ── decision record carries audit-able fields on every reject ──────────────


async def test_reject_always_carries_signal_id_reason_idempotency_key():
    acct = make_account(locked=True)
    d = await _decide(account=acct)
    assert d.signal_id is not None
    assert d.reason
    assert d.idempotency_key
    assert d.decided_at == NOW
    assert d.actor == "risk_engine"
