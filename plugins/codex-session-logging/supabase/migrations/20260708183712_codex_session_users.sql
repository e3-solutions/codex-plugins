create table if not exists public.codex_session_users (
  user_id uuid primary key,
  git_email text,
  git_user_name text,
  linear_user_name text,
  local_username text,
  hostname text,
  installation_id text,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists codex_session_users_git_email_idx
  on public.codex_session_users (lower(git_email))
  where git_email is not null;

create index if not exists codex_session_users_linear_user_name_idx
  on public.codex_session_users (lower(linear_user_name))
  where linear_user_name is not null;

create index if not exists codex_session_users_local_identity_idx
  on public.codex_session_users (local_username, hostname)
  where local_username is not null or hostname is not null;

create index if not exists codex_session_users_installation_id_idx
  on public.codex_session_users (installation_id)
  where installation_id is not null;

create or replace function public.codex_session_users_preserve_seen_at()
returns trigger
language plpgsql
as $$
begin
  new.first_seen_at = least(old.first_seen_at, new.first_seen_at);
  new.last_seen_at = greatest(old.last_seen_at, new.last_seen_at);
  new.created_at = old.created_at;
  new.updated_at = now();
  return new;
end;
$$;

revoke all on function public.codex_session_users_preserve_seen_at() from public;

drop trigger if exists codex_session_users_preserve_seen_at
  on public.codex_session_users;

create trigger codex_session_users_preserve_seen_at
  before update on public.codex_session_users
  for each row
  execute function public.codex_session_users_preserve_seen_at();

alter table public.codex_session_users enable row level security;

create policy "Users can read own Codex session user"
  on public.codex_session_users
  for select
  to authenticated
  using ((select auth.uid()) = user_id);

grant select on public.codex_session_users to authenticated;
grant select, insert, update on public.codex_session_users to service_role;
