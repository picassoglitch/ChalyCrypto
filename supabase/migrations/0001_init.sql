-- 0001_init.sql — chalybcrypto schema (tables only; RLS lives in 0002_rls.sql)
-- Run after auth.users exists. On Supabase it does natively; for local Postgres apply
-- supabase/test_auth_shim.sql first.

create schema if not exists chalybcrypto;
set search_path = chalybcrypto, public;

-- ── identity ──
create table if not exists users (
  id            uuid primary key references auth.users(id) on delete cascade,
  org_id        uuid,
  display_name  text,
  created_at    timestamptz not null default now()
);

-- ── connections (secrets are app-level encrypted blobs; never plaintext) ──
create table if not exists exchange_connections (
  id             uuid primary key default gen_random_uuid(),
  user_id        uuid not null references users(id) on delete cascade,
  exchange       text not null check (exchange in ('binance','lbank','bitunix')),
  api_key_enc    bytea not null,
  api_secret_enc bytea not null,
  ip_allowlist   text[],
  status         text not null default 'untested',
  last_tested_at timestamptz,
  created_at     timestamptz not null default now()
);
create index if not exists exchange_connections_user_id_idx on exchange_connections(user_id);

create table if not exists telegram_channels (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references users(id) on delete cascade,
  tg_channel_id text not null,
  title         text,
  enabled       boolean not null default true,
  score         numeric default 0,
  created_at    timestamptz not null default now()
);
create index if not exists telegram_channels_user_id_idx on telegram_channels(user_id);

-- ── fees (global; updatable from config seed) ──
create table if not exists fee_schedules (
  id            uuid primary key default gen_random_uuid(),
  exchange      text not null,
  symbol        text,
  vip_level     text default 'regular',
  maker_bps     numeric not null,
  taker_bps     numeric not null,
  effective_at  timestamptz not null default now(),
  source        text
);

-- ── signals ──
create table if not exists parsed_signals (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references users(id) on delete cascade,
  source        text not null,
  channel_id    uuid references telegram_channels(id),
  pair          text not null,
  side          text not null check (side in ('long','short')),
  entry         numeric,
  stop_loss     numeric,
  take_profits  numeric[],
  leverage      numeric,
  timeframe     text,
  margin_type   text,
  raw_text      text,
  dedup_hash    text not null,
  created_at    timestamptz not null default now()
);
create index if not exists parsed_signals_user_id_idx on parsed_signals(user_id);
create index if not exists parsed_signals_dedup_idx on parsed_signals(user_id, dedup_hash);

create table if not exists validated_signals (
  id            uuid primary key default gen_random_uuid(),
  parsed_id     uuid references parsed_signals(id) on delete cascade,
  user_id       uuid not null references users(id) on delete cascade,
  strategy      text,
  ev_net        numeric,
  decision      text not null check (decision in ('approved','rejected')),
  reject_reason text,
  snapshot_id   uuid,
  created_at    timestamptz not null default now()
);
create index if not exists validated_signals_user_id_idx on validated_signals(user_id);

-- ── execution ──
create table if not exists trades (
  id              uuid primary key default gen_random_uuid(),
  user_id         uuid not null references users(id) on delete cascade,
  exchange        text,
  pair            text,
  side            text,
  strategy        text,
  mode            text not null check (mode in ('backtest','paper','semi_auto','full_auto','breakeven_protection','manual_override')),
  entry_price     numeric,
  exit_price      numeric,
  qty             numeric,
  leverage        numeric,
  margin_type     text default 'isolated',
  fees_paid       numeric default 0,
  funding_paid    numeric default 0,
  realized_pnl    numeric,
  status          text,
  continue_flag   boolean default false,
  protected_floor numeric,
  opened_at       timestamptz default now(),
  closed_at       timestamptz
);
create index if not exists trades_user_id_idx on trades(user_id);
create index if not exists trades_user_status_idx on trades(user_id, status);

create table if not exists positions (
  id                uuid primary key default gen_random_uuid(),
  user_id           uuid not null references users(id) on delete cascade,
  trade_id          uuid references trades(id) on delete cascade,
  pair              text,
  side              text,
  qty               numeric,
  entry_price       numeric,
  liquidation_price numeric,
  unrealized_pnl    numeric,
  updated_at        timestamptz default now()
);
create index if not exists positions_user_id_idx on positions(user_id);

create table if not exists orders (
  id                uuid primary key default gen_random_uuid(),
  user_id           uuid not null references users(id) on delete cascade,
  trade_id          uuid references trades(id) on delete cascade,
  exchange_order_id text,
  type              text,
  side              text,
  price             numeric,
  qty               numeric,
  reduce_only       boolean default false,
  status            text,
  idempotency_key   text unique,
  created_at        timestamptz default now()
);
create index if not exists orders_user_id_idx on orders(user_id);

-- ── strategies / results / sims ──
create table if not exists strategies (
  id      uuid primary key default gen_random_uuid(),
  key     text unique not null,
  name    text,
  params  jsonb,
  enabled boolean default true
);

create table if not exists strategy_results (
  id             uuid primary key default gen_random_uuid(),
  user_id        uuid not null references users(id) on delete cascade,
  strategy       text,
  mode           text,
  sample_size    int,
  win_rate       numeric,
  profit_factor  numeric,
  avg_rr         numeric,
  max_drawdown   numeric,
  fee_drag       numeric,
  computed_at    timestamptz default now()
);
create index if not exists strategy_results_user_id_idx on strategy_results(user_id);

create table if not exists backtests (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references users(id) on delete cascade,
  strategy      text,
  pair          text,
  timeframe     text,
  period_start  timestamptz,
  period_end    timestamptz,
  params        jsonb,
  metrics       jsonb,
  optimistic    boolean not null default true,
  created_at    timestamptz default now()
);
create index if not exists backtests_user_id_idx on backtests(user_id);

create table if not exists paper_trades (
  id       uuid primary key default gen_random_uuid(),
  user_id  uuid not null references users(id) on delete cascade,
  trade_id uuid references trades(id) on delete cascade,
  notes    text
);
create index if not exists paper_trades_user_id_idx on paper_trades(user_id);

-- ── risk / mode gating ──
create table if not exists risk_profiles (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references users(id) on delete cascade,
  name        text,
  params      jsonb not null,
  is_default  boolean default false
);
create index if not exists risk_profiles_user_id_idx on risk_profiles(user_id);

create table if not exists mode_state (
  user_id                 uuid primary key references users(id) on delete cascade,
  mode                    text not null default 'paper',
  paper_trades_count      int default 0,
  paper_profit_factor     numeric,
  paper_max_drawdown      numeric,
  live_unlocked           boolean not null default false,
  account_protection_lock boolean not null default false,
  updated_at              timestamptz default now()
);

-- ── llm / market context / ops ──
create table if not exists ai_evaluations (
  id          uuid primary key default gen_random_uuid(),
  trade_id    uuid references trades(id) on delete cascade,
  model       text,
  kind        text,
  content     text,
  created_at  timestamptz default now()
);
create index if not exists ai_evaluations_trade_id_idx on ai_evaluations(trade_id);

create table if not exists market_snapshots (
  id        uuid primary key default gen_random_uuid(),
  pair      text,
  exchange  text,
  payload   jsonb,
  taken_at  timestamptz default now()
);

create table if not exists coinmarketcap_snapshots (
  id        uuid primary key default gen_random_uuid(),
  payload   jsonb,
  taken_at  timestamptz default now()
);

create table if not exists coinglass_snapshots (
  id        uuid primary key default gen_random_uuid(),
  pair      text,
  exchange  text,
  payload   jsonb,
  taken_at  timestamptz default now()
);

create table if not exists notifications (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references users(id) on delete cascade,
  channel    text,
  kind       text,
  payload    jsonb,
  status     text,
  created_at timestamptz default now()
);
create index if not exists notifications_user_id_idx on notifications(user_id);

create table if not exists audit_logs (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references users(id) on delete cascade,
  actor      text,
  action     text,
  reason     text,
  details    jsonb,
  created_at timestamptz default now()
);
create index if not exists audit_logs_user_id_idx on audit_logs(user_id);
