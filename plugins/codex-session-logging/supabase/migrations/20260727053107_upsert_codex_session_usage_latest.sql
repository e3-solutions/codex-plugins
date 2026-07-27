create table public.codex_rollout_replay_snapshots (
  snapshot_id uuid primary key default gen_random_uuid(),
  event_count bigint not null default 0 check (event_count >= 0),
  created_at timestamptz not null default now()
);

create table public.codex_rollout_replay_snapshot_events (
  snapshot_id uuid not null references public.codex_rollout_replay_snapshots(snapshot_id)
    on delete cascade,
  id uuid not null,
  session_id text not null,
  user_id uuid not null,
  storage_bucket text,
  storage_path text,
  metadata jsonb not null,
  primary key (snapshot_id, session_id, id)
);

create index codex_rollout_replay_snapshots_created_idx
  on public.codex_rollout_replay_snapshots (created_at);

alter table public.codex_rollout_replay_snapshots enable row level security;
alter table public.codex_rollout_replay_snapshot_events enable row level security;

revoke all privileges on public.codex_rollout_replay_snapshots
  from public, anon, authenticated, service_role;
revoke all privileges on public.codex_rollout_replay_snapshot_events
  from public, anon, authenticated, service_role;

grant select on public.codex_rollout_replay_snapshots to service_role;
grant select on public.codex_rollout_replay_snapshot_events to service_role;

create or replace function public.create_codex_rollout_replay_snapshot()
returns table (snapshot_id uuid, event_count bigint)
language plpgsql
security definer
set search_path = ''
as $$
declare
  captured_snapshot_id uuid := gen_random_uuid();
  captured_event_count bigint;
begin
  insert into public.codex_rollout_replay_snapshots (snapshot_id)
  values (captured_snapshot_id);

  insert into public.codex_rollout_replay_snapshot_events (
    snapshot_id,
    id,
    session_id,
    user_id,
    storage_bucket,
    storage_path,
    metadata
  )
  select
    captured_snapshot_id,
    event.id,
    event.session_id,
    event.user_id,
    event.storage_bucket,
    event.storage_path,
    event.metadata
  from public.codex_session_events as event
  where event.event_type = 'rollout_chunk';

  get diagnostics captured_event_count = row_count;

  update public.codex_rollout_replay_snapshots
  set event_count = captured_event_count
  where codex_rollout_replay_snapshots.snapshot_id = captured_snapshot_id;

  return query select captured_snapshot_id, captured_event_count;
end;
$$;

create or replace function public.delete_codex_rollout_replay_snapshot(
  p_snapshot_id uuid
)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  affected_rows integer;
begin
  delete from public.codex_rollout_replay_snapshots
  where snapshot_id = p_snapshot_id;
  get diagnostics affected_rows = row_count;
  return affected_rows > 0;
end;
$$;

revoke execute on function public.create_codex_rollout_replay_snapshot()
  from public, anon, authenticated;
revoke execute on function public.delete_codex_rollout_replay_snapshot(uuid)
  from public, anon, authenticated;

grant execute on function public.create_codex_rollout_replay_snapshot()
  to service_role;
grant execute on function public.delete_codex_rollout_replay_snapshot(uuid)
  to service_role;

alter table public.codex_session_usage
  add constraint codex_session_usage_additive_total_check
  check (
    input_tokens + cached_input_tokens + output_tokens + reasoning_output_tokens
      = total_tokens
  ) not valid;

create or replace function public.upsert_codex_session_usage_latest(
  p_session_id text,
  p_user_id uuid,
  p_input_tokens bigint,
  p_cached_input_tokens bigint,
  p_output_tokens bigint,
  p_reasoning_output_tokens bigint,
  p_total_tokens bigint,
  p_model_context_window bigint,
  p_observed_at timestamptz,
  p_metadata jsonb
)
returns boolean
language plpgsql
security invoker
set search_path = ''
as $$
declare
  affected_rows integer;
begin
  if p_input_tokens + p_cached_input_tokens + p_output_tokens
       + p_reasoning_output_tokens <> p_total_tokens then
    raise exception 'usage token components must sum exactly to total_tokens'
      using errcode = '22023';
  end if;

  insert into public.codex_session_usage (
    session_id,
    user_id,
    input_tokens,
    cached_input_tokens,
    output_tokens,
    reasoning_output_tokens,
    total_tokens,
    model_context_window,
    observed_at,
    metadata,
    updated_at
  )
  values (
    p_session_id,
    p_user_id,
    p_input_tokens,
    p_cached_input_tokens,
    p_output_tokens,
    p_reasoning_output_tokens,
    p_total_tokens,
    p_model_context_window,
    p_observed_at,
    coalesce(p_metadata, '{}'::jsonb),
    now()
  )
  on conflict (session_id) do update
  set
    user_id = excluded.user_id,
    input_tokens = excluded.input_tokens,
    cached_input_tokens = excluded.cached_input_tokens,
    output_tokens = excluded.output_tokens,
    reasoning_output_tokens = excluded.reasoning_output_tokens,
    total_tokens = excluded.total_tokens,
    model_context_window = coalesce(
      excluded.model_context_window,
      public.codex_session_usage.model_context_window
    ),
    observed_at = excluded.observed_at,
    metadata = excluded.metadata,
    updated_at = now()
  where
    excluded.total_tokens >= public.codex_session_usage.total_tokens
    and excluded.observed_at >= public.codex_session_usage.observed_at;

  get diagnostics affected_rows = row_count;
  return affected_rows > 0;
end;
$$;

revoke execute on function public.upsert_codex_session_usage_latest(
  text, uuid, bigint, bigint, bigint, bigint, bigint, bigint, timestamptz, jsonb
) from public, anon, authenticated;

grant execute on function public.upsert_codex_session_usage_latest(
  text, uuid, bigint, bigint, bigint, bigint, bigint, bigint, timestamptz, jsonb
) to service_role;
