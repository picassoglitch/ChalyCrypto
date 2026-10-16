-- 0006_signal_prefs.sql — Señales delivery preferences set from the Chalyb hub.
--
-- The hub's Señales screens (/app/senales) save how a person wants to be told
-- about signals: which coins to FILTER on, channels, timeframe, quiet hours.
-- Nothing here shapes a signal's content (hub REVISION-LEGAL C1): there is no
-- balance, holding, goal or risk field, and the API validates the shape
-- (chalybcrypto_api.hub_api.SignalPrefs, extra fields refused).
--
-- Written only through /api/hub/v1/signal-prefs (hub Bearer). One row per user.

set search_path = chalybcrypto, public;

create table if not exists signal_prefs (
  user_id     uuid primary key references users(id) on delete cascade,
  prefs       jsonb not null,
  updated_at  timestamptz not null default now()
);

alter table signal_prefs enable row level security;

do $$
begin
  if not exists (
    select 1 from pg_policies
     where schemaname = 'chalybcrypto' and tablename = 'signal_prefs'
       and policyname = 'signal_prefs_owner'
  ) then
    create policy signal_prefs_owner on signal_prefs
      using (user_id = auth.uid())
      with check (user_id = auth.uid());
  end if;
end $$;
