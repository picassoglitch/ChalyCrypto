-- 0002_rls.sql — Row-Level Security on every chalybcrypto table.
-- Pattern: per-user tables → owner-only via auth.uid() = user_id.
-- Global config tables (fee_schedules, strategies) → readable to all authed users,
-- writes restricted to service_role.
-- Global context snapshots (market/cmc/coinglass) → readable to all authed users,
-- writes restricted to service_role.
-- ai_evaluations joins through trades because it has no user_id of its own.
-- Supabase exposes service writes through the service_role JWT; we key those on the
-- standard `role` claim so this works against vanilla Postgres + our test auth shim too.

set search_path = chalybcrypto, public;

-- ──────────────────────────────────────────────────────────────────────────────
-- helper: which role is the current JWT?  (auth.role() exists on Supabase; fall
-- back to current_setting for local Postgres.)
-- ──────────────────────────────────────────────────────────────────────────────
create or replace function chalybcrypto.current_role_name() returns text
language sql stable as $$
  select coalesce(
    nullif(current_setting('request.jwt.claim.role', true), ''),
    nullif(current_setting('request.jwt.claims', true)::jsonb->>'role', ''),
    'anon'
  )
$$;

-- ──────────────────────────────────────────────────────────────────────────────
-- per-user tables
-- ──────────────────────────────────────────────────────────────────────────────

alter table users enable row level security;
create policy users_owner on users
  using (id = auth.uid())
  with check (id = auth.uid());

alter table exchange_connections enable row level security;
create policy exchange_connections_owner on exchange_connections
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

alter table telegram_channels enable row level security;
create policy telegram_channels_owner on telegram_channels
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

alter table parsed_signals enable row level security;
create policy parsed_signals_owner on parsed_signals
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

alter table validated_signals enable row level security;
create policy validated_signals_owner on validated_signals
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

alter table trades enable row level security;
create policy trades_owner on trades
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

alter table positions enable row level security;
create policy positions_owner on positions
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

alter table orders enable row level security;
create policy orders_owner on orders
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

alter table strategy_results enable row level security;
create policy strategy_results_owner on strategy_results
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

alter table backtests enable row level security;
create policy backtests_owner on backtests
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

alter table paper_trades enable row level security;
create policy paper_trades_owner on paper_trades
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

alter table risk_profiles enable row level security;
create policy risk_profiles_owner on risk_profiles
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

alter table mode_state enable row level security;
create policy mode_state_owner on mode_state
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

alter table notifications enable row level security;
create policy notifications_owner on notifications
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

alter table audit_logs enable row level security;
create policy audit_logs_owner on audit_logs
  using (user_id = auth.uid())
  with check (user_id = auth.uid());

-- ──────────────────────────────────────────────────────────────────────────────
-- ai_evaluations — visible only when the linked trade belongs to you
-- ──────────────────────────────────────────────────────────────────────────────

alter table ai_evaluations enable row level security;
create policy ai_evaluations_via_trade on ai_evaluations
  using (
    exists (
      select 1 from chalybcrypto.trades t
      where t.id = ai_evaluations.trade_id and t.user_id = auth.uid()
    )
  )
  with check (
    exists (
      select 1 from chalybcrypto.trades t
      where t.id = ai_evaluations.trade_id and t.user_id = auth.uid()
    )
  );

-- ──────────────────────────────────────────────────────────────────────────────
-- global config tables — readable to any authed user, service-role-only writes
-- ──────────────────────────────────────────────────────────────────────────────

alter table fee_schedules enable row level security;
create policy fee_schedules_read on fee_schedules
  for select using (auth.uid() is not null);
create policy fee_schedules_write on fee_schedules
  for all using (chalybcrypto.current_role_name() = 'service_role')
  with check (chalybcrypto.current_role_name() = 'service_role');

alter table strategies enable row level security;
create policy strategies_read on strategies
  for select using (auth.uid() is not null);
create policy strategies_write on strategies
  for all using (chalybcrypto.current_role_name() = 'service_role')
  with check (chalybcrypto.current_role_name() = 'service_role');

-- ──────────────────────────────────────────────────────────────────────────────
-- global context snapshots — readable to any authed user, service-role writes
-- (these are produced by scanners/workers, never by end users)
-- ──────────────────────────────────────────────────────────────────────────────

alter table market_snapshots enable row level security;
create policy market_snapshots_read on market_snapshots
  for select using (auth.uid() is not null);
create policy market_snapshots_write on market_snapshots
  for all using (chalybcrypto.current_role_name() = 'service_role')
  with check (chalybcrypto.current_role_name() = 'service_role');

alter table coinmarketcap_snapshots enable row level security;
create policy coinmarketcap_snapshots_read on coinmarketcap_snapshots
  for select using (auth.uid() is not null);
create policy coinmarketcap_snapshots_write on coinmarketcap_snapshots
  for all using (chalybcrypto.current_role_name() = 'service_role')
  with check (chalybcrypto.current_role_name() = 'service_role');

alter table coinglass_snapshots enable row level security;
create policy coinglass_snapshots_read on coinglass_snapshots
  for select using (auth.uid() is not null);
create policy coinglass_snapshots_write on coinglass_snapshots
  for all using (chalybcrypto.current_role_name() = 'service_role')
  with check (chalybcrypto.current_role_name() = 'service_role');
