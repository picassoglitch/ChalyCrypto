# ChalybCrypto — Architecture & Design Decisions

> Futures-only crypto trading engine. **Survival first, not profit promises.**
> This document is the "why". `CLAUDE.md` is the "rules". `BUILD_PLAN.md` is the "how/when".

---

## 0. Assumptions challenged (read before building)

The original spec is excellent on safety intent but contains several assumptions that will
break a real system. These corrections are **binding** unless the owner overrides them in writing.

### 0.1 LLMs must NOT be in the execution path
A scalper cannot wait 1–5s for an LLM and cannot accept non-deterministic go/no-go.
This also repeats a lesson already proven on Quantorpolybot: LLM directional scoring is a
documented dead end at retail scale (PolyBench / NeurIPS 2025 / LBS-Yale + empirical soaks).

**Decision:**
- Hot path = deterministic **Strategy Engine** + **Risk Engine** only. No network LLM calls.
- **Claude** = async *trading analyst*: writes the human-readable thesis, the "continue/exit"
  briefing, post-trade and daily summaries. Runs out-of-band, never gates a fill.
- **Qwen (local)** = optional async enrichment/labelling. Never gates a fill.
- If the LLM service is down, trading is unaffected; only explanations are delayed.

### 0.2 The scalping fee math is brutal — the product must say so
On the venues being kept, futures **taker = 0.06% per side → 0.12% round-trip of notional**
(LBank, Bitunix VIP0). At 50x that is **~6% of margin per round-trip in fees alone**, before
spread + slippage + funding. A 3-minute scalp must clear ~0.12% notional just to break even on
fees — *worse* than Binance's 0.10%, so dropping Binance makes this caution **stronger**, not weaker.

**Decision:** scalping is kept but framed as the *hardest, highest-risk* mode. Every trade must
pass an **EV-after-all-costs** gate (§5). "20% in 3 minutes" is never assumed or displayed as a target.
The default UX nudges toward "no trade" over "weak trade".

### 0.3 Data sourcing — three lanes (CMC alone can't do it)
CMC gives rankings, market cap, 24h volume, trending. It does **not** give futures funding rate,
open interest, order book, spread, or reliable intraday OHLCV. Those split across two other sources.

**Lane A — native exchange (EXECUTION-GRADE, real-time):** klines/OHLCV, **live order book depth**,
mark/index price, the funding rate *you actually pay on your venue*, and order placement.
- Bitunix: REST `/api/v1/futures/market/depth` + WebSocket `depth_books`, klines, funding.
- LBank: public `/depth` + kline REST, WebSocket depth.
- **The order book that gates a fill MUST come from here.** Never route a fill off a third-party book.
- Binance (OPTIONAL, **data-only**): dropped as a trading venue, but its free deep historical klines
  may still be pulled for **backtests** — public market data needs no account/keys/KYC and is *not*
  "trading on Binance". Recommended: keep it as a read-only backtest source, trade on LBank/Bitunix.

**Lane B — Coinglass (DERIVATIVES CONTEXT, cross-venue):** open interest (OHLC, dominance, anomaly
alerts), aggregated/weighted funding rates, **liquidation heatmaps**, long/short ratios. Covers both
LBank and Bitunix. Replaces the derivatives-context role; far better than CMC for it.
- Coinglass order book = L2/L3 **snapshots / heatmaps**, ≤1-min updates on lower tiers → usable as a
  *liquidity-zone context layer*, **NOT** for scalp execution timing.
- Licensing: ~$29 Hobbyist / $79 Startup / $299 Standard / $699 Pro. Lower tiers are **personal use,
  ≤1-min updates**; **commercial use (chalyb.com as a product) needs Standard $299/mo+.**

**Lane C — CoinMarketCap (MARKET CONTEXT):** ranking, market cap, 24h volume, trending, pair sanity
validation. Optional once Coinglass is in, but kept for ranking/trending (aligns with the
*"Aprende a usar el 100% de CoinMarketCap"* source doc).

### 0.4 Telegram has two distinct mechanisms
- **Reading arbitrary channels the user follows** → requires an MTProto **user session**
  (Telethon/Pyrogram) with the user's own credentials. The Bot API cannot read channels the bot
  doesn't administer.
- **Interactive control bot** (approve/close/breakeven buttons, notifications) → **Bot API**.

**Decision:** two components — `telegram_ingest` (Telethon, per-user session, read-only) and
`telegram_control` (Bot API, inline keyboards). Document the session-string security carefully.

### 0.5 Indicator pile-up is overfitting
Running 20+ indicators on every signal produces conflicting noise and false confidence.

**Decision:** each strategy uses a **small set of orthogonal filters** (a trend filter, a
location/structure filter, a trigger, a cost gate). Indicators are computed on demand per strategy,
not all-at-once "validation soup". The full indicator list in the spec is the *library*, not the
per-trade checklist.

### 0.6 Backtests lie about scalping
Naive OHLCV backtests assume perfect fills, no spread, no funding → wildly optimistic for HFT-style
trades.

**Decision:** the backtester applies **conservative fills** (taker fees both sides by default,
configurable spread, slippage model, funding accrual on holds), labels every result
"BACKTEST = OPTIMISTIC", and requires a minimum paper-trade sample before a strategy is allowed live.

### 0.7 Full-auto is the highest-liability feature
**Decision:** full-auto is **out of the MVP**. Ship Backtest → Paper → Semi-auto. Full-auto is a
later, heavily gated phase and may stay internal-only for the public chalyb.com product.

### 0.8 Distribution through chalyb.com has a regulatory surface
Offering automated execution/signals to LATAM retail may constitute regulated investment advice or
asset management (e.g. CNBV in Mexico). Users-bring-own-API-keys (no custody, no pooled funds) is a
strong mitigation, but it does not remove the question. **Get a compliance/legal review before
selling this to third parties.** (This is a flag, not legal advice.)

---

## 1. High-level architecture

```
                         ┌─────────────────────────────────────────┐
                         │  chalyb.com (Next.js 15, existing)     │
                         │  /operators/chalybcrypto  (RBAC-gated)      │
                         │  setup wizard + dashboard module          │
                         └───────────────┬───────────────────────────┘
                                         │ authed REST + Supabase Realtime/SSE
                                         ▼
┌──────────────┐   REST/SSE   ┌────────────────────┐   tasks   ┌────────────────────┐
│ services/api │◀────────────▶│  services/engine    │◀─────────▶│  services/worker    │
│ FastAPI      │              │  strategy + risk +  │  Redis    │  Celery + beat:     │
│ auth, routes │              │  execution (sync,   │  broker   │  scanners, signal   │
│              │              │  deterministic)     │  + locks  │  ingest, schedules  │
└──────┬───────┘              └─────────┬───────────┘           └─────────┬──────────┘
       │                                │                                  │
       │                                ▼                                  ▼
       │                     ┌────────────────────┐         ┌────────────────────────┐
       │                     │ exchange connectors │         │ telegram_ingest (MTProto)│
       │                     │ Binance/LBank/Bitunix│        │ telegram_control (BotAPI)│
       │                     │ + CMC connector     │         └────────────────────────┘
       │                     └────────────────────┘
       ▼
┌─────────────────────────────────────────────────────────────────┐
│ Supabase (Postgres + Auth + Realtime + RLS)  schema: chalybcrypto   │
└─────────────────────────────────────────────────────────────────┘
       ▲
       │ async (never blocks a fill)
┌──────┴───────────────┐
│ LLM layer            │
│ Claude = analyst     │  explanations, trade plans, continue/exit briefings, daily digest
│ Qwen   = enrichment  │
└──────────────────────┘
```

**Stack:** Next.js 15 + TS + Tailwind v4 (reuse chalyb.com) · FastAPI (Python 3.12) ·
Supabase (Postgres/Auth/Realtime) · Redis + Celery · TradingView Lightweight Charts ·
Docker Compose (local) → cloud later.

---

## 2. The hot path (deterministic, sub-second)

```
candidate (scanner OR telegram signal OR manual)
   → build MarketSnapshot   (exchange klines, funding, OI, order book, spread, ATR, etc.)
   → Strategy.evaluate(snapshot)  → Signal{side, entry, sl, tps, lev, thesis_tags}  | None
   → EV gate (§5)                 → reject if EV_net ≤ min_expected_profit
   → Risk Engine.authorize(...)   → APPROVE | REJECT(reason)   ← FINAL AUTHORITY
   → mode router:
        backtest/paper → simulated fill
        semi-auto      → enqueue human approval (Telegram + dashboard)
        full-auto      → (phase later) execute directly
   → persist signal/decision + full audit log
   → fire-and-forget: enqueue Claude explanation job
```

Strategies are **pure functions** `evaluate(MarketSnapshot, params) -> Signal | None`. Same code path
for backtest, paper, and live — only the fill source differs. This guarantees backtest↔live parity.

---

## 3. Strategy library (MVP picks 2–3 orthogonal ones)

Full library (build incrementally): EMA trend scalping, RSI reversal, MACD momentum confirm,
ADX trend-strength filter, VWAP mean reversion, breakout-retest, S/R bounce, FVG, order block,
liquidity sweep, divergence, ATR volatility breakout, market-structure-shift (MSS), change-of-character
(CHoCH), order-book imbalance, funding-rate-aware, CMC momentum/liquidity filter, Telegram-signal-validation.

**MVP set (orthogonal, all covered by your source docs):**
1. **Trend-follow:** EMA(35/50) trend + ADX≥threshold filter + market-structure confirmation
   (left-side-of-chart rule, ≥200 candles) — mirrors your `technical_pattern_scout` lane.
2. **Mean-reversion:** VWAP deviation + RSI exhaustion, regime-gated (only when ADX is *low*).
3. **ICT/structure:** FVG + Order Block + liquidity sweep (from your *FVGs & Order Blocks* doc).

Telegram-copied signals are **candidates**, not auto-trades: they enter the same pipeline and must
pass the same strategy validation + EV + risk gates.

Every strategy supports: backtest, paper, live, perf tracking, fee/slippage/funding sim,
configurable params, risk rules, explainable thesis. No strategy goes live with <N validated paper
trades (configurable, default 50) and a positive net profit factor in paper.

---

## 4. Risk Engine — final authority

Runs **last**, deterministic, fully logged, can veto Claude/strategy/human. Components:
position sizing, leverage calc, **liquidation estimator + min liquidation distance check**,
margin requirement, max-loss calc, breakeven manager, trailing-stop manager, **protected-profit-stop
manager (§6)**, max-drawdown guard, daily/weekly loss guards, exchange-API-failure protection,
stale-price protection (reject if last tick > T seconds old), rate-limit protection, idempotency /
duplicate-order prevention (Redis lock keyed on dedup hash), account-protection lock (global kill).

Hard locks (cannot be bypassed in any mode):
- daily loss / weekly loss / max drawdown reached → **lock new entries**, manage existing only.
- liquidation distance < configured min → reject.
- stale price / connector failure / rate-limited → reject (fail safe, never fail open).
- live mode while paper-gate unmet → reject (enforced in DB, §BUILD_PLAN).

Configurable rules (per risk_profile): max risk/trade, max daily/weekly loss, max drawdown,
max open positions, max leverage, max exposure per asset, max total exposure, max trades/hour,
min RR, min ADX, min liquidity, min volume, min expected profit after fees, cooldown after loss,
cooldown after volatility spike, duplicate prevention, breakeven trigger, trailing trigger,
partial-TP trigger, auto-SL adjustment, account-protection lock.

---

## 5. Expected-value gate (the cost-honesty rule)

A candidate is eligible only if:

```
EV_net = p_win*avg_win - p_loss*avg_loss
         - round_trip_fees(notional, exchange, vip)
         - expected_spread_cost
         - expected_slippage
         - expected_funding(hold_time)
EV_net > risk_profile.min_expected_profit_after_fees
```

- `p_win`, `avg_win`, `avg_loss` come from the strategy's **own validated stats** (backtest seeds it,
  paper confirms it). No hand-typed guesses.
- If sample size < threshold → EV is considered **unknown** → risk engine rejects for live/semi-auto
  (paper/backtest still allowed to gather data).
- Fees are read from the `fee_schedules` table (per exchange/symbol/VIP, updatable from config),
  never hardcoded.

Base fees seeded in config (override anytime):
| Exchange | Maker | Taker |
|---|---|---|
| Binance Futures (regular) | 0.02% | 0.05% |
| LBank Futures | 0.02% | 0.06% |
| Bitunix Futures (VIP0) | 0.02% | 0.06% |

---

## 6. Breakeven & protected-profit stop (precise definition)

The spec example: "up $100, continue → protect at least $70". Made precise and **ratcheting**:

- **Breakeven move:** when unrealized PnL ≥ `breakeven_trigger`, move SL to entry **+ fees buffer**
  (so breakeven means net-zero after exit fees, not gross-zero).
- **Protected-profit stop (on "continue"):**
  - On activation, snapshot `peak_gain = current_unrealized_pnl_net`.
  - `protected_floor = peak_gain * (1 - giveback)` where `giveback` default = 0.30 (configurable).
  - As price makes new highs, `peak_gain` ratchets up and `protected_floor` ratchets up with it.
    It **never decreases**.
  - The floor is enforced as **net** profit (after exit taker fee + accrued funding), so the
    protected amount is what actually lands in the account.
  - If net unrealized PnL touches `protected_floor` → **close immediately** (reduce-only market or
    a resting reduce-only stop, per config).
- Implemented in `RiskEngine.protected_profit_manager`, evaluated on every price tick for open
  positions flagged `continue=true`. Fully audit-logged on every ratchet and on trigger.

---

## 7. Modes

`backtest` · `paper` · `semi_auto` (bot proposes, human approves) · `breakeven_protection`
(core configurable behavior, replaces "emergency stop") · `manual_override`.
`full_auto` exists as an enum/flag but is **disabled in MVP** and unreachable until a later phase
and the paper-gate + risk criteria are met.

**Live-gate (enforced in DB + code):** a user/account cannot enter `semi_auto` or `full_auto` for a
given strategy until: ≥ `min_paper_trades` completed, paper net profit factor ≥ threshold, max paper
drawdown ≤ threshold, and connections tested green. State lives in `mode_state` / `paper_trading_gates`.

---

## 8. Security

- Exchange API keys: **trade-only permissions, withdrawals disabled, IP allowlist**. Encrypted at
  rest (app-level envelope encryption; key from env/KMS, never in DB plaintext). Never logged, never
  returned to client after save.
- Telegram MTProto session strings: same treatment as API keys.
- Supabase RLS on every table keyed to `auth.uid()` / org membership.
- No secret ever appears in logs, audit logs, or Claude/Qwen prompts.
- All exchange writes go through idempotency keys to prevent duplicate orders on retries.

---

## 9. Non-goals (explicit)

- No spot trading. **Futures only.**
- No custody of user funds. Users bring their own exchange API keys.
- No guaranteed-profit messaging anywhere in UI, docs, or notifications.
- No LLM in the execution decision path.
