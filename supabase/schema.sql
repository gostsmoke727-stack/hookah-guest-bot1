create table if not exists public.guest_memory (
  user_id bigint primary key,
  name text,
  username text,
  style text,
  likes jsonb not null default '[]'::jsonb,
  dislikes jsonb not null default '[]'::jsonb,
  allergies jsonb not null default '[]'::jsonb,
  usual_strength text,
  strength_level integer,
  usual_bowl text,
  favorite_flavors jsonb not null default '[]'::jsonb,
  favorite_mixes jsonb not null default '[]'::jsonb,
  last_hookahs jsonb not null default '[]'::jsonb,
  visit_count integer not null default 0,
  last_seen timestamptz
);

alter table public.guest_memory enable row level security;

revoke all on public.guest_memory from anon, authenticated;
grant all on public.guest_memory to service_role;
