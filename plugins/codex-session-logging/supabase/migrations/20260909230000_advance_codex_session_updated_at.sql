create or replace function public.advance_codex_session_updated_at(
  p_session_id text,
  p_user_id text
)
returns timestamptz
language plpgsql
security definer
set search_path = ''
as $function$
declare
  next_updated_at timestamptz;
begin
  update public.codex_sessions
  set updated_at = greatest(
    pg_catalog.clock_timestamp(),
    updated_at + interval '1 microsecond'
  )
  where id = p_session_id
    and user_id::text = p_user_id
  returning updated_at into next_updated_at;

  if next_updated_at is null then
    raise no_data_found using
      message = 'owned Codex session was not found';
  end if;

  return next_updated_at;
end;
$function$;

revoke all on function public.advance_codex_session_updated_at(text, text)
  from public, anon, authenticated;
grant execute on function public.advance_codex_session_updated_at(text, text)
  to service_role;
