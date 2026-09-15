-- 0003_approvals.sql — semi-auto approval queue.
--
-- An approval row is the durable representation of a signal the risk engine
-- approved while the operator was running in semi_auto mode. The human (or
-- Telegram control bot) flips its status to approved/rejected/continued/etc.
-- before any exchange write happens. CLAUDE.md rule 8: every state change is
-- idempotency-keyed.

set search_path = chalybcrypto, public;

create table if not exists approvals (
  id            uuid primary key default gen_random_uuid(),
  user_id       uuid not null references users(id) on delete cascade,

  -- In-memory Signal.id at the moment risk engine ran. Not an FK — parsed_signals
  -- rows live on a separate id space; this lets us correlate when both exist.
  signal_id     uuid,

  -- Self-contained snapshot of the order intent. Display + resolution don't
  -- need to JOIN anything else.
  pair          text not null,
  side          text not null check (side in ('long','short')),
  entry         numeric,
  stop_loss     numeric,
  take_profits  numeric[],
  leverage      numeric,
  qty           numeric,
  ev_net_bps    numeric,
  liquidation_distance_bps numeric,
  strategy      text,

  -- Idempotency: same dedup_hash + decision reason → same key (set by risk engine).
  idempotency_key text not null unique,

  -- State machine.
  status        text not null default 'pending'
    check (status in ('pending','approved','rejected','continued','closed',
                      'breakeven','protected','expired')),
  resolved_at   timestamptz,
  resolved_by   text,        -- 'human' | 'auto' | 'expired' | 'telegram'
  resolution_reason text,

  created_at    timestamptz not null default now()
);

create index if not exists approvals_user_id_idx on approvals(user_id);
create index if not exists approvals_user_status_idx on approvals(user_id, status);
create index if not exists approvals_pending_idx on approvals(user_id, created_at)
  where status = 'pending';

-- RLS — same owner-only pattern as the rest of the per-user tables.
alter table approvals enable row level security;
create policy approvals_owner on approvals
  using (user_id = auth.uid())
  with check (user_id = auth.uid());
