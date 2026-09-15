"""Parser demo — feeds it 5 real-world-shaped signals and prints what comes out."""

import sys

# Windows consoles default to cp1252 — reconfigure stdout for the emoji-laced samples.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass

from chalybcrypto_telegram_ingest import parse_signal


SAMPLES = [
    # English, full fields
    "🚀 BTCUSDT LONG\nEntry: 60000\nSL: 59500\nTP1: 60500\nTP2: 61000\nLeverage 10x\n5m timeframe",
    # Spanish "compra en"
    "ETH/USDT COMPRA en 3500. Stop pérdida 3450. Objetivo 3600. Apalancamiento 5x.",
    # Spanish vender / a — short
    "VENDER SOLUSDT a 150. SL 155. TP 140. 8x.",
    # European decimal comma + cross margin
    "BTC-USDT SHORT entry 60050,75 SL 60500,5 TP1 59500 cross 15x 15m",
    # Ambiguous — should be rejected
    "BTCUSDT LONG or SHORT — wait for confirmation",
]

for i, msg in enumerate(SAMPLES, 1):
    parsed = parse_signal(msg)
    print(f"\n[{i}] {msg.strip()[:80]}")
    if parsed is None:
        print("    -> rejected (parser refused to guess)")
        continue
    print(f"    -> {parsed.pair} {parsed.side.value} "
          f"entry={parsed.entry} sl={parsed.stop_loss} tps={parsed.take_profits} "
          f"lev={parsed.leverage} tf={parsed.timeframe} margin={parsed.margin_type}")
