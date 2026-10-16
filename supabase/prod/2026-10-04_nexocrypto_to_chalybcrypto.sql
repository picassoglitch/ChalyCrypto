-- One-off for the shared Chalyb Supabase project (uqcbziwdgbnzehipzjxp).
-- NOT a migration: run once, by hand, with the owner's OK, BEFORE deploying an
-- image that talks to Postgres (claude/senales-golive or later).
--
--   npx supabase db query --linked -f supabase/prod/2026-10-04_nexocrypto_to_chalybcrypto.sql
--
-- Why: prod has migrations 0001–0004 under the pre-rebrand schema name
-- `nexocrypto`; the code (pg_store.py) and 0005 use `chalybcrypto`.
-- Checked read-only on 2026-10-04: 23 tables, RLS on all, 5 users + 5 tenants,
-- every other table empty; the only function (current_role_name) doesn't name
-- the schema; the only FK leaves the schema to auth.users; no views elsewhere
-- reference it. Policies and FKs follow the rename (they hold OIDs).
--
-- Rollback (before the new image is deployed):
--   alter schema chalybcrypto rename to nexocrypto;  -- usage_outbox comes along; drop it if wanted

begin;

do $$
begin
  if exists (select 1 from pg_namespace where nspname = 'chalybcrypto') then
    raise exception 'schema chalybcrypto already exists; stop and look';
  end if;
  if not exists (select 1 from pg_namespace where nspname = 'nexocrypto') then
    raise exception 'schema nexocrypto not found; stop and look';
  end if;
end $$;

alter schema nexocrypto rename to chalybcrypto;

-- 0005_usage_outbox.sql, verbatim.
set local search_path = chalybcrypto, public;

create table if not exists usage_outbox (
  id                bigserial primary key,
  source_id         text not null unique,
  external_user_id  text not null,
  reservation_id    text,
  event             jsonb not null,
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

commit;

-- Post-checks (expect: 24 tables, rls everywhere, tenants = 5):
--   select count(*), bool_and(c.relrowsecurity) from pg_class c
--     join pg_namespace n on n.oid = c.relnamespace
--    where n.nspname = 'chalybcrypto' and c.relkind = 'r';
--   select count(*) from chalybcrypto.tenants;
