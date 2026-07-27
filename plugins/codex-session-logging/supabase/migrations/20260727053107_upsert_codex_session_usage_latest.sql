alter table public.codex_session_usage
  add constraint codex_session_usage_additive_total_check
  check (
    input_tokens + cached_input_tokens + output_tokens + reasoning_output_tokens
      = total_tokens
  ) not valid;

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
  session_owner uuid;
  usage_owner uuid;
begin
  if p_input_tokens + p_cached_input_tokens + p_output_tokens
       + p_reasoning_output_tokens <> p_total_tokens then
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
    and excluded.total_tokens >= public.codex_session_usage.total_tokens
    and excluded.observed_at >= public.codex_session_usage.observed_at;

  get diagnostics affected_rows = row_count;
  return affected_rows > 0;
end;
$$;

revoke execute on function public.upsert_codex_session_usage_latest(
  text, bigint, bigint, bigint, bigint, bigint, bigint, timestamptz, jsonb
) from public, anon, authenticated, service_role;

grant execute on function public.upsert_codex_session_usage_latest(
  text, bigint, bigint, bigint, bigint, bigint, bigint, timestamptz, jsonb
) to service_role;
