create or replace function public.fence_owned_codex_session(
  p_session_id text,
  p_user_id text,
  p_storage_bucket text,
  p_storage_prefix text
)
returns jsonb
language plpgsql
security definer
set search_path = ''
as $function$
declare
  session_hash text := encode(
    extensions.digest(
      'codex_rollout_session:' || p_session_id,
      'sha256'
    ),
    'hex'
  );
  expected_prefix text :=
    'users/' || p_user_id || '/sessions/' || p_session_id;
  parent_exists boolean;
  locator_exists boolean;
  locator_prefix text;
  locator_prefix_count bigint;
begin
  if p_user_id is null or p_user_id = ''
     or p_storage_bucket is null or p_storage_bucket = ''
     or p_storage_prefix is distinct from expected_prefix then
    raise invalid_parameter_value using
      message = 'session storage locator is invalid';
  end if;
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      'codex_rollout_session:' || p_session_id,
      0
    )
  );

  if exists (
    select 1
    from public.codex_ignored_sessions ignored
    where ignored.session_id_hash = session_hash
  ) then
    return jsonb_build_object('status', 'fenced');
  end if;

  select exists (
    select 1 from public.codex_sessions session
    where session.id = p_session_id
  ) into parent_exists;
  select exists (
    select 1 from public.codex_session_storage_locators locator
    where locator.session_id_hash = session_hash
  ) into locator_exists;
  if locator_exists then
    select min(locator.storage_prefix), count(distinct locator.storage_prefix)
    into locator_prefix, locator_prefix_count
    from public.codex_session_storage_locators locator
    where locator.session_id_hash = session_hash;
    if locator_prefix_count <> 1 then
      raise check_violation using
        message = 'session storage identity conflicts';
    end if;
  end if;

  if exists (
    select 1
    from public.codex_sessions session
    where session.id = p_session_id
      and session.user_id::text is distinct from p_user_id
  ) or exists (
    select 1
    from public.codex_session_storage_locators locator
    where locator.session_id_hash = session_hash
      and locator.user_id is distinct from p_user_id
  ) then
    raise exception 'session ownership does not match'
      using errcode = '42501';
  end if;

  if not parent_exists and not locator_exists then
    insert into public.codex_ignored_sessions(session_id_hash, ignored_at)
    values (session_hash, pg_catalog.clock_timestamp());
    return jsonb_build_object('status', 'fenced');
  end if;

  if not locator_exists then
    insert into public.codex_session_storage_locators(
      session_id_hash,
      user_id,
      storage_bucket,
      storage_prefix
    ) values (
      session_hash,
      p_user_id,
      p_storage_bucket,
      p_storage_prefix
    );
  end if;

  if parent_exists then
    update public.codex_sessions
    set updated_at = pg_catalog.clock_timestamp(),
        metadata = coalesce(metadata, '{}'::jsonb)
          || pg_catalog.jsonb_build_object('ignore_extension_fenced', true)
    where id = p_session_id
      and user_id::text = p_user_id;
  else
    insert into public.codex_sessions(
      id,
      user_id,
      storage_prefix,
      metadata,
      updated_at
    ) values (
      p_session_id,
      p_user_id::uuid,
      coalesce(locator_prefix, p_storage_prefix),
      pg_catalog.jsonb_build_object(
        'source', 'rollout_sync',
        'ignore_extension_fenced', true
      ),
      pg_catalog.clock_timestamp()
    );
  end if;

  insert into public.codex_ignored_sessions(session_id_hash, ignored_at)
  values (session_hash, pg_catalog.clock_timestamp());

  return jsonb_build_object('status', 'fenced');
end;
$function$;

revoke all on function public.fence_owned_codex_session(text, text, text, text)
  from public, anon, authenticated;
grant execute on function public.fence_owned_codex_session(text, text, text, text)
  to service_role;
