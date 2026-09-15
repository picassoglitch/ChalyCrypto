from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from chalybcrypto_shared import MarginType, Side
from chalybcrypto_telegram_ingest import parse_signal


NOW = datetime(2026, 6, 6, 12, 0, tzinfo=timezone.utc)


def test_empty_or_blank_returns_none():
    assert parse_signal("") is None
    assert parse_signal("   \n  ") is None


def test_missing_pair_returns_none():
    assert parse_signal("LONG entry 100 SL 95 TP 110") is None


def test_missing_side_returns_none():
    """Pair present but no clear direction."""
    assert parse_signal("BTCUSDT entry 60000") is None


def test_basic_english_long_signal_parses_all_fields():
    msg = """
    🚀 BTCUSDT LONG
    Entry: 60000
    SL: 59500
    TP1: 60500
    TP2: 61000
    Leverage 10x
    Timeframe 5m
    """
    s = parse_signal(msg, now=NOW)
    assert s is not None
    assert s.pair == "BTCUSDT"
    assert s.side == Side.LONG
    assert s.entry == Decimal("60000")
    assert s.stop_loss == Decimal("59500")
    assert s.take_profits == [Decimal("60500"), Decimal("61000")]
    assert s.leverage == Decimal("10")
    assert s.timeframe == "5m"
    assert s.dedup_hash


def test_spanish_compra_parses_as_long():
    msg = "ETH/USDT COMPRA en 3500. Stop pérdida 3450. Objetivo 3600. Apalancamiento 5x."
    s = parse_signal(msg)
    assert s is not None
    assert s.pair == "ETHUSDT"
    assert s.side == Side.LONG
    assert s.entry == Decimal("3500")
    assert s.stop_loss == Decimal("3450")
    assert s.take_profits == [Decimal("3600")]
    assert s.leverage == Decimal("5")


def test_spanish_vender_parses_as_short():
    msg = "VENDER SOLUSDT a 150. SL 155. TP 140. 8x."
    s = parse_signal(msg)
    assert s is not None
    assert s.pair == "SOLUSDT"
    assert s.side == Side.SHORT
    assert s.entry == Decimal("150")
    assert s.stop_loss == Decimal("155")
    assert s.take_profits == [Decimal("140")]
    assert s.leverage == Decimal("8")


def test_ambiguous_long_and_short_returns_none():
    """If a message says both LONG and SHORT, refuse to guess."""
    msg = "BTCUSDT LONG or SHORT?"
    assert parse_signal(msg) is None


def test_pair_separators_normalized():
    assert parse_signal("BTC-USDT LONG").pair == "BTCUSDT"
    assert parse_signal("BTC_USDT LONG").pair == "BTCUSDT"
    assert parse_signal("BTC/USDT LONG").pair == "BTCUSDT"


def test_european_decimal_comma_handled():
    s = parse_signal("BTCUSDT LONG entry 60000,50 SL 59500,25")
    assert s is not None
    assert s.entry == Decimal("60000.50")
    assert s.stop_loss == Decimal("59500.25")


def test_isolated_margin_detected():
    s = parse_signal("BTCUSDT LONG entry 60000 isolated 10x")
    assert s.margin_type == MarginType.ISOLATED


def test_cross_margin_detected():
    s = parse_signal("BTCUSDT SHORT entry 60000 cross 5x")
    assert s.margin_type == MarginType.CROSS


def test_partial_message_still_parses_with_optional_fields_none():
    """Pair + side present, nothing else — still useful as a candidate."""
    s = parse_signal("BTCUSDT LONG")
    assert s is not None
    assert s.entry is None
    assert s.stop_loss is None
    assert s.take_profits == []
    assert s.leverage is None


def test_hourly_and_daily_timeframes():
    s1 = parse_signal("BTCUSDT LONG 1h")
    assert s1.timeframe == "1h"
    s2 = parse_signal("BTCUSDT LONG entry 60000 1d hold")
    assert s2.timeframe == "1d"


def test_dedup_hash_stable_for_same_inputs():
    a = parse_signal("BTCUSDT LONG entry 60000 SL 59500", now=NOW)
    b = parse_signal("BTCUSDT LONG entry 60000 SL 59500", now=NOW)
    assert a.dedup_hash == b.dedup_hash


def test_dedup_hash_differs_when_entry_differs():
    a = parse_signal("BTCUSDT LONG entry 60000", now=NOW)
    b = parse_signal("BTCUSDT LONG entry 60100", now=NOW)
    assert a.dedup_hash != b.dedup_hash
