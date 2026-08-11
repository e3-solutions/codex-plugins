create or replace function public.list_ignored_codex_session_objects(
  p_session_id text,
  p_limit integer default 1000
)
returns jsonb
language plpgsql
stable
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
  items jsonb;
begin
  if not public.codex_session_is_ignored(p_session_id) then
    raise insufficient_privilege using
      message = 'session is not fenced for deletion';
  end if;
  if p_limit is null or p_limit < 1 or p_limit > 1000 then
    raise invalid_parameter_value using
      message = 'p_limit must be between 1 and 1000';
  end if;
  select coalesce(
    jsonb_agg(
      jsonb_build_object('bucket', object.bucket_id, 'path', object.name)
      order by object.bucket_id, object.name
    ),
    '[]'::jsonb
  )
  into items
  from (
    select object_for_locator.bucket_id, object_for_locator.name
    from public.codex_session_storage_locators locator
    cross join lateral (
      select stored.bucket_id, stored.name
      from storage.objects stored
      where stored.bucket_id = locator.storage_bucket
        and stored.name collate "C" >=
          (locator.storage_prefix || '/') collate "C"
        and stored.name collate "C" <
          (locator.storage_prefix || '0') collate "C"
      limit p_limit
    ) object_for_locator
    where locator.session_id_hash = session_hash
    limit p_limit
  ) object;
  return jsonb_build_object(
    'status', case when jsonb_array_length(items) = 0 then 'empty' else 'pending' end,
    'items', items
  );
end;
$function$;

revoke all on function public.list_ignored_codex_session_objects(text, integer)
  from public, anon, authenticated;
grant execute on function public.list_ignored_codex_session_objects(text, integer)
  to service_role;

create or replace function public.finalize_ignored_codex_session_purge(
  p_session_id text
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
  fenced_at timestamptz;
  deleted_count bigint;
begin
  select ignored.ignored_at
  into fenced_at
  from public.codex_ignored_sessions ignored
  where ignored.session_id_hash = session_hash;
  if not found then
    raise insufficient_privilege using
      message = 'session is not fenced for deletion';
  end if;
  if fenced_at > pg_catalog.clock_timestamp() - interval '10 minutes' then
    return jsonb_build_object('status', 'pending_quiescence');
  end if;
  if exists (
    select 1
    from public.codex_session_storage_locators locator
    cross join lateral (
      select 1
      from storage.objects stored
      where stored.bucket_id = locator.storage_bucket
        and stored.name collate "C" >=
          (locator.storage_prefix || '/') collate "C"
        and stored.name collate "C" <
          (locator.storage_prefix || '0') collate "C"
      limit 1
    ) pending_object
    where locator.session_id_hash = session_hash
  ) then
    return jsonb_build_object('status', 'pending_storage');
  end if;
  delete from public.codex_sessions where id = p_session_id;
  get diagnostics deleted_count = row_count;
  delete from public.codex_session_storage_locators
  where session_id_hash = session_hash;
  return jsonb_build_object(
    'status', case when deleted_count = 1 then 'deleted' else 'already_absent' end
  );
end;
$function$;

revoke all on function public.finalize_ignored_codex_session_purge(text)
  from public, anon, authenticated;
grant execute on function public.finalize_ignored_codex_session_purge(text)
  to service_role;
