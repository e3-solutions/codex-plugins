create or replace function public.codex_sessions_preserve_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $function$
begin
  new.updated_at = pg_catalog.greatest(old.updated_at, new.updated_at);
  return new;
end;
$function$;

revoke all on function public.codex_sessions_preserve_updated_at()
  from public, anon, authenticated;

drop trigger if exists codex_sessions_preserve_updated_at
  on public.codex_sessions;
create trigger codex_sessions_preserve_updated_at
  before update on public.codex_sessions
  for each row
  execute function public.codex_sessions_preserve_updated_at();

create or replace function public.publish_codex_session_change(
  p_session_id text,
  p_user_id text
)
returns timestamptz
language plpgsql
security invoker
set search_path = ''
as $function$
declare
  published_at timestamptz;
begin
  if p_session_id is null or p_session_id = ''
     or p_user_id is null or p_user_id = '' then
    raise invalid_parameter_value using
      message = 'session id and user id are required';
  end if;

  update public.codex_sessions as session
  set updated_at = pg_catalog.greatest(
    session.updated_at + interval '1 microsecond',
    pg_catalog.clock_timestamp()
  )
  where session.id = p_session_id
    and session.user_id::text = p_user_id
  returning session.updated_at into published_at;

  if published_at is null then
    raise no_data_found using
      message = 'owned session was not found';
  end if;

  return published_at;
end;
$function$;

revoke all on function public.publish_codex_session_change(text, text)
  from public, anon, authenticated;
grant execute on function public.publish_codex_session_change(text, text)
  to service_role;
