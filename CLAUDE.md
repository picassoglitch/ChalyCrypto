# CLAUDE.md — ChalyCrypto build rules

You are building **ChalyCrypto**, a **futures-only** crypto trading engine. Read `ARCHITECTURE.md`
and `BUILD_PLAN.md` before writing code. The user has also attached source documents (trading books
and Spanish-language strategy/risk PDFs) — treat those as the **trading-knowledge source of truth**
for strategy logic, risk concepts, and terminology.

## Source documents (attached by the user)
- *Handbook of Trading*, *How to Profit from Cryptos*.
- Spanish docs: *Plan de Recuperación y Blindaje*, *Prompt Semáforo de Riesgo*,
  *Guía de Principiantes de Estructura de Mercado*, *Indicadores Técnicos Esenciales Gratuitos*,
  *Herramientas Salario Infinito*, *Mejora tu Trading con estos 3 Consejos*,
  *Aprende a usar el 100% de CoinMarketCap*, *FVGs y Order Blocks*, *Tipos de Divergencia*.
- TradingRiot.com: only legal/publicly-available concepts. Do **not** reproduce paid course
  material; encode the *concepts* in your own implementation.
Index these into `/docs/knowledge/` as your-own-words summaries + a concept→strategy mapping. Never
copy long passages verbatim.

## Non-negotiable rules (do not violate, do not "optimize away")
1. **Futures only.** Never scaffold spot trading.
2. **No LLM in the execution path.** Strategy + Risk engines are deterministic and own go/no-go.
   Claude/Qwen run async for explanations only. If you find yourself awaiting an LLM before a fill,
   stop — that's the bug.
3. **Risk Engine has final authority.** It runs last and can reject anything. Fail **safe** (reject),
   never fail open.
4. **No guaranteed-profit language** anywhere — UI, copy, notifications, comments, docs.
5. **Paper-before-live is enforced in code + DB**, not just UI. `full_auto` is disabled in MVP.
6. **Fees/spread/slippage/funding** are always in the math. Fees come from the `fee_schedules`
   table, never hardcoded inline.
7. **Secrets** (exchange keys, Telegram session strings) are encrypted at rest, never logged, never
   placed in any LLM prompt, never returned to the client after creation.
8. **Idempotency** on every exchange write (Redis lock on dedup hash).
9. Every trade decision writes an **audit log** row with the full reason (including rejections).

## Tech + conventions
- Python 3.12, FastAPI, Pydantic v2, async where it helps, type hints everywhere, `ruff` + `black`.
- Next.js 15 + TypeScript + Tailwind v4, **Spanish-first** via `next-intl` (LATAM audience), `en` fallback.
- Supabase Postgres, dedicated `chalybcrypto` schema, **RLS on every table**.
- Redis + Celery (broker + beat). Charts: TradingView Lightweight Charts.
- Tests required for: risk engine, EV gate, fee math, liquidation estimator, protected-profit stop,
  signal parser. Use `pytest`. A phase isn't "done" until its tests pass (see BUILD_PLAN acceptance).
- Connectors implement a common `ExchangeConnector` ABC. Trading venues = **LBank + Bitunix** (build
  **Bitunix first** — clean REST + WS depth). Binance is **optional, data-only** (free historical klines
  for backtests; no keys, no live trading). Add a **Coinglass** context connector (OI/funding/liquidations).
- Commit per milestone with a clear message. Keep PRs small and reviewable.

## Engine semantics
- Strategies: pure `evaluate(MarketSnapshot, params) -> Signal | None`. Same path for
  backtest/paper/live; only the fill source changes.
- `MarketSnapshot` sources from three lanes (ARCHITECTURE §0.3): **native exchange** (LBank/Bitunix)
  for klines + **live order book** + venue funding + execution — the fill-gating book is ALWAYS native;
  **Coinglass** for derivatives context (OI, aggregated funding, liquidation heatmaps, long/short — its
  order book is ≤1-min snapshots, context only, never for fills); **CoinMarketCap** for ranking/trending.
- Protected-profit stop is **ratcheting** and enforced on **net** PnL (see ARCHITECTURE §6).
- EV gate uses the strategy's own validated stats; unknown stats (low sample) → reject for live.

## Style of work
- Start each phase by restating its acceptance criteria, then build, then show the passing tests.
- When the spec and these rules conflict, **these rules win** — say so explicitly and continue.
- Prefer "no feature" over a feature that weakens a safety rule. Surface trade-offs; don't hide them.
- Keep the UI honest: rejected signals always show the reason; backtests are labelled OPTIMISTIC.

## What "done" means for the MVP
Backtest → Paper → Semi-auto on **one** exchange (Bitunix), 2–3 strategies, full risk engine,
Telegram ingest + control bot, dashboard mounted inside chalyb.com, Docker Compose up locally,
all listed tests green. Full-auto, multi-exchange live, and the long-tail strategies come after.
