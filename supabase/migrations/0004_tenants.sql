-- 0004_tenants.sql — Chalyb tenant mapping.
--
-- A tenant row is ChalybCrypto's view of a Chalyb user. Created by the admin
-- provisioning endpoint when the user clicks "Abrir ChalybCrypto" for the first
-- time. external_user_id is Chalyb's user_id (UUID); tenant_id (this table's
-- id) is what chalyb stores in engine_subscriptions and references afterwards.
--
-- api_token is a service-to-service token returned to chalyb on provision.
-- It's not a user secret — it's how the chalyb backend authenticates to
-- ChalybCrypto's admin API on behalf of the user. Stored as-is for v1 (we can
-- rotate via the status endpoint later).
--
-- Writers: only the admin router (service-role context). No user-facing RLS
-- policy because end users never touch this table directly.

set search_path = chalybcrypto, public;

create table if not exists tenants (
  id                uuid primary key default gen_random_uuid(),
  external_user_id  text not null unique,
  email             text not null,
  display_name      text,
  tier              text not null default 'free' check (tier in ('free','pro','all_access')),
  status            text not null default 'active' check (status in ('active','paused')),
  api_token         text not null,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

create index if not exists tenants_external_user_id_idx on tenants(external_user_id);

-- RLS: deny everything by default; the admin router runs with service-role and
-- bypasses RLS, which is the only path that should write here.
alter table tenants enable row level security;
