create table public.codex_session_usage_observations (
  id uuid primary key,
  session_id text not null
    references public.codex_sessions(id) on delete cascade,
  user_id uuid not null,
  input_tokens bigint not null check (input_tokens >= 0),
  cached_input_tokens bigint not null check (cached_input_tokens >= 0),
  output_tokens bigint not null check (output_tokens >= 0),
  reasoning_output_tokens bigint not null
    check (reasoning_output_tokens >= 0),
  observed_at timestamptz not null,
  metadata jsonb not null default '{}'::jsonb
);

comment on table public.codex_session_usage_observations is
  'Append-only cumulative token observations. observed_at is event time; '
  'range consumers use (start, end], subtract the latest snapshots at or '
  'before each boundary, and keep cached input separate. A zero baseline is '
  'valid only when codex_sessions.started_at >= start; otherwise coverage '
  'is incomplete.';

do $$
begin
  if not exists (
    select 1
    from pg_catalog.pg_roles
    where rolname = 'codestat_ro'
  ) then
    create role codestat_ro nologin;
  end if;
end
$$;

create index codex_session_usage_observations_observed_session_idx
  on public.codex_session_usage_observations (
    observed_at desc,
    session_id
  );

create index codex_session_usage_observations_session_observed_idx
  on public.codex_session_usage_observations (
    session_id,
    observed_at desc,
    id desc
  );

alter table public.codex_session_usage_observations
  enable row level security;

create policy "Users can read own Codex session usage observations"
  on public.codex_session_usage_observations
  for select
  to authenticated
  using ((select auth.uid()) = user_id);

create policy "Codestat can read Codex session usage observations"
  on public.codex_session_usage_observations
  for select
  to codestat_ro
  using (true);

create policy "Codestat can read Codex sessions"
  on public.codex_sessions
  for select
  to codestat_ro
  using (true);

create policy "Codestat can read Codex session messages"
  on public.codex_session_messages
  for select
  to codestat_ro
  using (true);

create policy "Codestat can read Codex session users"
  on public.codex_session_users
  for select
  to codestat_ro
  using (true);

revoke all privileges
  on public.codex_session_usage_observations
  from anon, authenticated, service_role, codestat_ro;

grant select
  on public.codex_session_usage_observations
  to authenticated;

grant select, insert
  on public.codex_session_usage_observations
  to service_role;

grant select
  on public.codex_session_usage_observations,
    public.codex_sessions,
    public.codex_session_messages,
    public.codex_session_users
  to codestat_ro;

create or replace function public.upsert_codex_session_usage_latest(
  p_session_id text,
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
  observation_digest bytea;
  observation_hex text;
  observation_id uuid;
  session_owner uuid;
  usage_owner uuid;
begin
  if p_input_tokens < 0
    or p_cached_input_tokens < 0
    or p_output_tokens < 0
    or p_reasoning_output_tokens < 0
    or p_total_tokens < 0
    or (p_model_context_window is not null and p_model_context_window < 0)
  then
    raise exception 'usage token values must be non-negative'
      using errcode = '22023';
  end if;

  if p_input_tokens::numeric + p_cached_input_tokens::numeric
       + p_output_tokens::numeric + p_reasoning_output_tokens::numeric
       <> p_total_tokens::numeric then
    raise exception 'usage token components must sum exactly to total_tokens'
      using errcode = '22023';
  end if;

  select user_id
  into session_owner
  from public.codex_sessions
  where id = p_session_id;
  if not found then
    raise exception 'Codex session % does not exist', p_session_id
      using errcode = '23503';
  end if;

  select user_id
  into usage_owner
  from public.codex_session_usage
  where session_id = p_session_id;
  if found and usage_owner <> session_owner then
    raise exception 'Codex session usage owner conflicts with session owner'
      using errcode = '23514';
  end if;

  observation_digest := pg_catalog.sha256(
    pg_catalog.convert_to(
      pg_catalog.jsonb_build_array(
        p_session_id,
        pg_catalog.to_char(
          p_observed_at at time zone 'UTC',
          'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
        ),
        p_input_tokens,
        p_cached_input_tokens,
        p_output_tokens,
        p_reasoning_output_tokens
      )::text,
      'UTF8'
    )
  );
  observation_digest := pg_catalog.set_byte(
    observation_digest,
    6,
    (pg_catalog.get_byte(observation_digest, 6) & 15) | 80
  );
  observation_digest := pg_catalog.set_byte(
    observation_digest,
    8,
    (pg_catalog.get_byte(observation_digest, 8) & 63) | 128
  );
  observation_hex := pg_catalog.encode(
    pg_catalog.substring(observation_digest from 1 for 16),
    'hex'
  );
  observation_id := (
    pg_catalog.substring(observation_hex from 1 for 8) || '-'
    || pg_catalog.substring(observation_hex from 9 for 4) || '-'
    || pg_catalog.substring(observation_hex from 13 for 4) || '-'
    || pg_catalog.substring(observation_hex from 17 for 4) || '-'
    || pg_catalog.substring(observation_hex from 21 for 12)
  )::uuid;

  insert into public.codex_session_usage_observations (
    id,
    session_id,
    user_id,
    input_tokens,
    cached_input_tokens,
    output_tokens,
    reasoning_output_tokens,
    observed_at,
    metadata
  )
  values (
    observation_id,
    p_session_id,
    session_owner,
    p_input_tokens,
    p_cached_input_tokens,
    p_output_tokens,
    p_reasoning_output_tokens,
    p_observed_at,
    coalesce(p_metadata, '{}'::jsonb)
  )
  on conflict (id) do nothing;

  if not exists (
    select 1
    from public.codex_session_usage_observations
    where id = observation_id
      and session_id = p_session_id
      and user_id = session_owner
      and input_tokens = p_input_tokens
      and cached_input_tokens = p_cached_input_tokens
      and output_tokens = p_output_tokens
      and reasoning_output_tokens = p_reasoning_output_tokens
      and observed_at = p_observed_at
  ) then
    raise exception 'usage observation id conflicts with different data'
      using errcode = '23514';
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
    session_owner,
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
    public.codex_session_usage.user_id = session_owner
    and excluded.input_tokens >= public.codex_session_usage.input_tokens
    and excluded.cached_input_tokens
      >= public.codex_session_usage.cached_input_tokens
    and excluded.output_tokens >= public.codex_session_usage.output_tokens
    and excluded.reasoning_output_tokens
      >= public.codex_session_usage.reasoning_output_tokens
    and excluded.total_tokens >= public.codex_session_usage.total_tokens
    and excluded.observed_at >= public.codex_session_usage.observed_at;

  get diagnostics affected_rows = row_count;
  return affected_rows > 0;
end;
$$;

revoke execute
  on function public.upsert_codex_session_usage_latest(
    text, bigint, bigint, bigint, bigint, bigint, bigint, timestamptz, jsonb
  )
  from public, anon, authenticated, service_role;

grant execute
  on function public.upsert_codex_session_usage_latest(
    text, bigint, bigint, bigint, bigint, bigint, bigint, timestamptz, jsonb
  )
  to service_role;
