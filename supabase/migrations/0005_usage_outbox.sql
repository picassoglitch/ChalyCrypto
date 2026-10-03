-- 0005_usage_outbox.sql — durable outbox for Chalyb hub usage events.
--
-- Consumption contract ("Delivery"): every metered event (llm.tokens, …) is
-- written here first, then drained to POST {hub}/api/engines/chalybcrypto/usage
-- in batches of ≤100 with exponential backoff. A permanent 4xx flips the row to
-- 'dead' (kept, never deleted) so it can be inspected and requeued by hand.
--
-- source_id is the hub's idempotency key ((engine, source_id) unique there), so
-- re-sending a row after a crash is always safe.
--
-- Writers: backend only (service-role). No user-facing policy: end users never
-- read or write this table.

set search_path = chalybcrypto, public;

create table if not exists usage_outbox (
  id                bigserial primary key,
  source_id         text not null unique,
  external_user_id  text not null,
  reservation_id    text,
  event             jsonb not null,           -- wire shape of one /usage event
  status            text not null default 'pending'
                      check (status in ('pending','sent','dead')),
  attempts          integer not null default 0,
  next_attempt_at   timestamptz not null default now(),
  last_error        text,
  created_at        timestamptz not null default now(),
  sent_at           timestamptz
);

create index if not exists usage_outbox_due_idx
  on usage_outbox (next_attempt_at)
  where status = 'pending';

alter table usage_outbox enable row level security;
