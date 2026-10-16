-- 0007_signal_feed.sql — the Señales shared feed (feed.py, feed_job.py).
--
-- One general reading per coin, the same for every user: there is NO user
-- column on purpose (hub REVISION-LEGAL C1). No entry, stop, take-profit or
-- leverage either: only the direction, the confidence and plain text about
-- the price. Written by the feed job, read by /api/hub/v1/feed (service
-- role). RLS on with no policy: end users never read it directly.

set search_path = chalybcrypto, public;

create table if not exists signal_feed (
  id             uuid primary key default gen_random_uuid(),
  coin           text not null,
  pair           text not null,
  interval       text not null,
  state          text not null check (state in ('buy', 'sell', 'wait')),
  confidence     text not null check (confidence in ('high', 'medium', 'none')),
  why_short      text not null,
  why_bullets    jsonb not null,
  explanation    text not null,
  strategies     jsonb not null default '[]'::jsonb,
  ref_price_usd  numeric not null,
  ref_price_mxn  numeric,
  created_at     timestamptz not null default now()
);

create index if not exists signal_feed_coin_created_idx on signal_feed (coin, created_at desc);
create index if not exists signal_feed_created_idx on signal_feed (created_at desc);

alter table signal_feed enable row level security;
