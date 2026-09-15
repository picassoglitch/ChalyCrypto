# BUILD_PLAN.md — phased build for Claude Code

Build in phases. **Do not jump ahead.** Each phase has acceptance criteria and a copy-paste prompt.
Commit at the end of each phase. The risk engine lands early because it's the safety moat.

Repo layout target:
```
chalybcrypto/
├── CLAUDE.md  ARCHITECTURE.md  BUILD_PLAN.md  INTEGRATION_chalyb.md
├── docker-compose.yml  .env.example
├── docs/knowledge/                 # your-own-words summaries of the attached docs
├── packages/shared/                # pydantic models, fee tables, enums, dedup hashing
├── services/
│   ├── api/                        # FastAPI: auth, routes, SSE
│   ├── engine/                     # strategy + risk + execution (deterministic)
│   ├── worker/                     # Celery tasks + beat (scanners, ingest, schedules)
│   ├── telegram_ingest/            # Telethon MTProto reader (per-user session)
│   └── telegram_control/           # Bot API: notifications + inline-keyboard controls
├── connectors/                     # ExchangeConnector ABC + binance/lbank/bitunix + cmc
├── llm/                            # async Claude analyst + Qwen enrichment (out of hot path)
├── web/                            # Next.js module (or mount into chalyb.com — see INTEGRATION)
└── supabase/                       # schema.sql, migrations, RLS policies
```

---

## Phase 0 — Scaffold & contracts
**Goal:** repo skeleton, shared models, config, Docker, no business logic yet.
- `packages/shared`: `MarketSnapshot`, `Signal`, `TradeDecision`, `RiskProfile`, enums (Mode, Side,
  MarginType, OrderType), `FeeSchedule`, dedup-hash util.
- Config loader (env → typed settings). `fee_schedules` seeded from config.
- `docker-compose.yml` brings up api + redis + (local supabase or hosted) and a healthcheck.
**Done when:** `docker compose up` is green, `/api/health` returns ok, shared models import + a model
round-trip test passes.

> **Prompt:** "Scaffold Phase 0 per BUILD_PLAN and CLAUDE.md. Create packages/shared with the listed
> pydantic v2 models and enums, the typed config loader seeding fee_schedules, the FastAPI app with
> /api/health, and docker-compose. Add a pytest that round-trips each model. Don't add trading logic."

## Phase 1 — Supabase schema + RLS
**Goal:** `chalybcrypto` schema from `supabase/schema.sql`, RLS on every table, migrations.
**Done when:** migrations apply clean; RLS test proves a user can't read another user's rows.

> **Prompt:** "Apply supabase/schema.sql as migrations, add RLS policies keyed to auth.uid()/org for
> every table, and write a test that cross-user reads are denied."

## Phase 2 — Connector abstraction + Bitunix/LBank + Coinglass + CMC
**Goal:** `ExchangeConnector` ABC (klines, ticker, **live order book depth**, venue funding, mark price,
balances, place/cancel/reduce-only order, position info). Implement **Bitunix first** (REST
`/api/v1/futures/market/depth` + `depth_books` WS), then **LBank** (`/depth`, kline, WS depth). Add a
**Coinglass** context connector (OI, aggregated funding, liquidation heatmaps, long/short) and a **CMC**
connector (ranking/trending). Optional: a **Binance data-only** kline fetcher for backtests (public, no keys).
**Done when:** Bitunix (testnet / live read-only) fetches klines + depth + funding + places/cancels a
reduce-only order; Coinglass returns OI/funding for the same symbol; CMC returns ranking; connector tests
pass with recorded fixtures (CI never hits live APIs). The fill-gating book is the **native** book.

> **Prompt:** "Build connectors/ per ARCHITECTURE §0.3 and CLAUDE rules. Bitunix connector first
> (REST + WebSocket depth), then LBank, a Coinglass context connector (OI/funding/liquidations), a CMC
> ranking connector, and an optional Binance public-kline fetcher for backtest data only. Recorded
> fixtures for tests. Never log keys. The execution order book must be the native venue book."

## Phase 3 — Risk Engine (the moat) ⚠️ highest priority
**Goal:** everything in ARCHITECTURE §4–6: position sizing, leverage, liquidation estimator + min
distance, margin calc, EV gate (§5), max-loss, daily/weekly/drawdown guards, cooldowns, idempotency,
stale-price + connector-failure fail-safe, breakeven manager, **ratcheting protected-profit stop**,
account-protection lock. Pure functions over `MarketSnapshot` + `RiskProfile` + account state.
**Done when:** exhaustive pytest suite passes, including: EV gate rejects negative-EV-after-costs;
liquidation distance rejection; protected-profit floor ratchets up and never down and protects **net**;
daily-loss lock blocks new entries but allows managing open ones; stale price → reject; unknown-stats →
reject for live.

> **Prompt:** "Implement services/engine/risk per ARCHITECTURE §4–6 with the full pytest suite listed
> in BUILD_PLAN Phase 3. Deterministic, fail-safe, fully audit-logged. This must be airtight."

## Phase 4 — Strategy Engine + backtester + paper engine
**Goal:** `Strategy` ABC (`evaluate -> Signal|None`), the 3 MVP strategies (EMA/ADX trend,
VWAP/RSI mean-reversion, FVG/OB/liquidity-sweep). Backtester with conservative fills (fees both sides,
spread, slippage, funding accrual), labelled OPTIMISTIC. Paper engine reusing the same hot path with
simulated fills. Per-strategy performance tracking feeding the EV gate's stats.
**Done when:** each strategy backtests on Binance historical klines, produces a thesis + metrics
(win rate, profit factor, RR, max DD, fee drag), and paper-trades through the full pipeline incl. risk
engine. Backtest results are clearly labelled and persisted to `backtests`/`strategy_results`.

> **Prompt:** "Build services/engine/strategy + backtester + paper engine per ARCHITECTURE §2–5.
> Same evaluate() path for backtest/paper/live. Encode strategy logic from docs/knowledge summaries.
> Conservative fills, funding accrual, OPTIMISTIC labels, metrics persisted."

## Phase 5 — Telegram ingest + control bot
**Goal:** `telegram_ingest` (Telethon, per-user session, read unlimited channels), parser that
normalizes messy signals → `parsed_signals` (pair, side, entry, SL, TPs, leverage, timeframe, margin,
source). Channel scoring from realized results. `telegram_control` (Bot API) for notifications +
inline buttons: Approve / Reject / Continue / Close / Move SL to BE / Activate protected stop / Pause.
Parsed signals enter the **same** pipeline as candidates (validation + EV + risk).
**Done when:** sample messy messages parse correctly (parser test fixtures), a copied signal flows
through validation → risk → semi-auto approval queue, and control buttons drive engine actions.

> **Prompt:** "Build services/telegram_ingest (Telethon) and services/telegram_control (Bot API) per
> ARCHITECTURE §0.4. Parser with fixture tests for messy formats, channel scoring, inline-keyboard
> controls wired to engine actions. Secure session-string handling."

## Phase 6 — API + LLM analyst (async) + scanners
**Goal:** FastAPI routes (§API_ROUTES below), SSE/Realtime feed, Celery scanners (futures scan, ADX/
trend scan, CMC context), and the **async Claude analyst** (trade plans, continue/exit briefings,
daily digest) + optional Qwen enrichment — strictly out of the hot path.
**Done when:** scanners produce candidates on schedule; semi-auto approval round-trip works end to end;
Claude explanations attach to trades without ever blocking a decision (kill the LLM service → trading
still works).

## Phase 7 — Setup wizard + dashboard (mounted in chalyb.com)
**Goal:** wizard (Telegram connect → exchange keys → exchange select → CMC key → risk profile →
mode select → notifications → **test all connections** → require paper before live). Dashboard:
equity, balance, open/closed positions, unrealized/realized PnL, drawdown, win rate, profit factor,
avg RR, PnL by exchange/strategy/Telegram-channel, active signals, **rejected signals with reason**,
CMC scanner, ADX/trend scanner, human approval queue, Telegram + API connection status, logs.
See `INTEGRATION_chalyb.md` for mounting it as an operator engine.
**Done when:** wizard blocks live mode until the paper-gate is satisfied; dashboard reads live via
Supabase Realtime/SSE; everything is RBAC-gated inside chalyb.com.

## Phase 8 (later, gated) — Full-auto, multi-exchange live, long-tail strategies
Only after Phases 0–7 are stable, paper-gates proven, and (for distribution) a compliance review.

---

## API routes (first pass)
```
POST /api/health
# connections
POST /api/connections/exchange         # save encrypted keys (trade-only), test
POST /api/connections/telegram         # store session (ingest) + bot link (control)
POST /api/connections/cmc
POST /api/connections/test             # test all
# config
GET/PUT /api/risk-profiles
GET/PUT /api/fee-schedules
GET/PUT /api/mode                       # enforces paper-gate before semi/full auto
# strategies / data
GET  /api/strategies
POST /api/backtests                     # run + persist (OPTIMISTIC-labelled)
GET  /api/strategy-results
# signals / trades
GET  /api/signals?status=parsed|validated|rejected
GET  /api/approvals                     # semi-auto queue
POST /api/approvals/{id}/decision       # approve|reject|continue|close|breakeven|protect
GET  /api/positions  /api/trades  /api/orders
# realtime
GET  /api/stream                        # SSE: telemetry, new signals, approvals, fills
# llm (async, non-blocking)
POST /api/explain/{trade_id}
GET  /api/digest/daily
```
