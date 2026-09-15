from __future__ import annotations

from decimal import Decimal

from chalybcrypto_engine.risk import RejectReason
from chalybcrypto_engine.risk.ev import (
    EVInputs,
    ev_net_bps,
    ev_passes,
    expected_funding_bps,
    round_trip_fees_bps,
)
from chalybcrypto_shared import FeeSchedule, Mode

from ._helpers import NOW, make_ev_inputs, make_stats


def test_round_trip_fees_taker_both_sides():
    inputs = make_ev_inputs(taker_bps=Decimal("6"))
    assert round_trip_fees_bps(inputs) == Decimal("12")


def test_funding_charges_at_least_one_cycle_for_any_hold():
    inputs = make_ev_inputs(hold_hours=Decimal("0.01"), funding_rate=Decimal("0.0001"))
    bps = expected_funding_bps(inputs)
    assert bps == Decimal("1")  # 0.0001 * 10000 = 1 bp per cycle


def test_funding_rounds_up_partial_cycles():
    # 9h hold @ 8h intervals → ceil(9/8) = 2 cycles
    inputs = make_ev_inputs(hold_hours=Decimal("9"), funding_rate=Decimal("0.0001"))
    assert expected_funding_bps(inputs) == Decimal("2")


def test_negative_ev_is_negative():
    # 50/50 strategy with avg_win=20 avg_loss=20 has 0 EV before costs; costs push it negative.
    stats = make_stats(win_rate=Decimal("0.5"), avg_win_bps=Decimal("20"), avg_loss_bps=Decimal("20"))
    inputs = make_ev_inputs()
    assert ev_net_bps(stats, inputs) < 0


def test_ev_gate_rejects_negative_ev_after_costs_for_live():
    stats = make_stats(win_rate=Decimal("0.5"), avg_win_bps=Decimal("20"), avg_loss_bps=Decimal("20"))
    ok, reason, bps = ev_passes(
        stats, make_ev_inputs(), mode=Mode.SEMI_AUTO,
        min_expected_profit_after_fees_bps=Decimal("5"),
    )
    assert ok is False
    assert reason == RejectReason.EV_NEGATIVE_AFTER_COSTS
    assert bps is not None and bps < 0


def test_ev_gate_unknown_stats_rejected_for_live():
    ok, reason, _ = ev_passes(
        None, make_ev_inputs(), mode=Mode.SEMI_AUTO,
        min_expected_profit_after_fees_bps=Decimal("5"),
    )
    assert ok is False
    assert reason == RejectReason.EV_STATS_UNKNOWN


def test_ev_gate_low_sample_rejected_for_live():
    stats = make_stats(sample_size=10)  # below default min_sample_for_live=50
    ok, reason, _ = ev_passes(
        stats, make_ev_inputs(), mode=Mode.SEMI_AUTO,
        min_expected_profit_after_fees_bps=Decimal("5"),
    )
    assert ok is False
    assert reason == RejectReason.EV_STATS_UNKNOWN


def test_ev_gate_unknown_stats_allowed_for_backtest_and_paper():
    for mode in (Mode.BACKTEST, Mode.PAPER):
        ok, reason, _ = ev_passes(
            None, make_ev_inputs(), mode=mode,
            min_expected_profit_after_fees_bps=Decimal("5"),
        )
        assert ok is True, f"unknown stats must be allowed for {mode} to gather data"
        assert reason == RejectReason.OK


def test_ev_gate_passes_positive_strategy():
    stats = make_stats(win_rate=Decimal("0.55"), avg_win_bps=Decimal("80"), avg_loss_bps=Decimal("40"))
    ok, reason, bps = ev_passes(
        stats, make_ev_inputs(taker_bps=Decimal("2"), spread_bps=Decimal("0"), slippage_bps=Decimal("0")),
        mode=Mode.SEMI_AUTO,
        min_expected_profit_after_fees_bps=Decimal("5"),
    )
    assert ok is True
    assert reason == RejectReason.OK
    assert bps is not None and bps > Decimal("5")
