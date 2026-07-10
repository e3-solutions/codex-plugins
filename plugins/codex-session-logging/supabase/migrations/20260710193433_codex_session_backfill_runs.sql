create table if not exists public.codex_session_backfill_runs (
  user_id uuid not null,
  installation_id text not null,
  backfill_version integer not null check (backfill_version > 0),
  status text not null check (status in ('running', 'partial', 'complete', 'failed')),
  files_discovered integer not null default 0 check (files_discovered >= 0),
  files_processed integer not null default 0 check (files_processed >= 0),
  records_queued integer not null default 0 check (records_queued >= 0),
  files_skipped_non_e3 integer not null default 0 check (files_skipped_non_e3 >= 0),
  files_failed integer not null default 0 check (files_failed >= 0),
  remaining_files integer not null default 0 check (remaining_files >= 0),
  started_at timestamptz,
  completed_at timestamptz,
  last_heartbeat_at timestamptz not null default now(),
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (user_id, installation_id, backfill_version)
);

create index if not exists codex_session_backfill_runs_status_heartbeat_idx
  on public.codex_session_backfill_runs (status, last_heartbeat_at desc);

alter table public.codex_session_backfill_runs enable row level security;

create policy "Users can read own Codex session backfill runs"
  on public.codex_session_backfill_runs
  for select
  to authenticated
  using ((select auth.uid()) = user_id);

revoke all privileges on public.codex_session_backfill_runs from anon, authenticated;
revoke all privileges on public.codex_session_backfill_runs from service_role;

grant select on public.codex_session_backfill_runs to authenticated;
grant select, insert, update on public.codex_session_backfill_runs to service_role;

create or replace function public.codex_sessions_preserve_started_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.started_at = least(old.started_at, new.started_at);
  new.created_at = old.created_at;
  return new;
end;
$$;

revoke all on function public.codex_sessions_preserve_started_at() from public;

drop trigger if exists codex_sessions_preserve_started_at on public.codex_sessions;
create trigger codex_sessions_preserve_started_at
  before update on public.codex_sessions
  for each row
  execute function public.codex_sessions_preserve_started_at();
