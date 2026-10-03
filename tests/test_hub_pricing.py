"""Hub price table → cost_usd_micros (consumption contract, 'Billing unit')."""

from __future__ import annotations

import logging

from chalybcrypto_hub import llm_cost_usd_micros, llm_tokens_event, normalize_model


def test_haiku_input_and_output_rates():
    # $1/MTok in, $5/MTok out → 1 µ$ and 5 µ$ per token.
    assert llm_cost_usd_micros("claude-haiku-4-5", input_tokens=1_000_000) == 1_000_000
    assert llm_cost_usd_micros("claude-haiku-4-5", output_tokens=1_000_000) == 5_000_000


def test_haiku_cache_rates_are_multiples_of_input():
    # cache read 0.1× input, cache write 1.25× input.
    assert llm_cost_usd_micros("claude-haiku-4-5", cache_read_tokens=1_000_000) == 100_000
    assert llm_cost_usd_micros("claude-haiku-4-5", cache_write_tokens=1_000_000) == 1_250_000


def test_mixed_call_matches_hand_computation():
    # 1500 in + 80 out + 1400 cache write + 0 cache read on Haiku:
    # 1500*1 + 80*5 + 1400*1.25 = 1500 + 400 + 1750 = 3650 µ$
    cost = llm_cost_usd_micros(
        "claude-haiku-4-5",
        input_tokens=1500,
        output_tokens=80,
        cache_write_tokens=1400,
    )
    assert cost == 3650


def test_fractional_micros_round_up():
    # 3 cache-read tokens = 0.3 µ$ → 1 (never under-bill)
    assert llm_cost_usd_micros("claude-haiku-4-5", cache_read_tokens=3) == 1
    assert llm_cost_usd_micros("claude-haiku-4-5") == 0


def test_dated_model_id_is_normalized():
    assert normalize_model("claude-haiku-4-5-20251001") == "claude-haiku-4-5"
    assert llm_cost_usd_micros("claude-haiku-4-5-20251001", output_tokens=10) == 50


def test_unknown_model_logs_error_and_uses_highest_rate(caplog):
    with caplog.at_level(logging.ERROR, logger="chalybcrypto_hub.pricing"):
        cost = llm_cost_usd_micros("claude-mystery-9", input_tokens=1000, output_tokens=1000)
    # Highest known: $5 in / $25 out (opus-4-8 row) → 5000 + 25000
    assert cost == 30_000
    assert any("unknown model" in r.message for r in caplog.records)
    assert cost > llm_cost_usd_micros("claude-haiku-4-5", input_tokens=1000, output_tokens=1000)


def test_llm_tokens_event_shape():
    e = llm_tokens_event(
        external_user_id="u1",
        model="claude-haiku-4-5",
        input_tokens=1500,
        output_tokens=80,
        cache_read_tokens=10,
        cache_write_tokens=1400,
        operation="analyst.thesis",
        reservation_id="r1",
    )
    wire = e.to_wire()
    assert wire["kind"] == "llm.tokens"
    assert wire["provider"] == "anthropic"
    assert wire["amount"] == 1500 + 80 + 10 + 1400
    assert wire["metadata"]["tokens"] == {
        "input": 1500,
        "output": 80,
        "cache_read": 10,
        "cache_write": 1400,
    }
    assert wire["cost_usd_micros"] == 3651  # 3650 + ceil(1.0)
    assert wire["reservation_id"] == "r1"
    assert wire["source_id"].startswith("llm_")
    assert isinstance(wire["cost_usd_micros"], int)
