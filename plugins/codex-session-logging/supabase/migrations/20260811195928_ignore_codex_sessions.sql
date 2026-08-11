create table if not exists public.codex_ignored_sessions (
  session_id_hash text primary key
    check (session_id_hash ~ '^[0-9a-f]{64}$'),
  ignored_at timestamptz not null default clock_timestamp()
);

create table if not exists public.codex_session_storage_locators (
  session_id_hash text not null
    check (session_id_hash ~ '^[0-9a-f]{64}$'),
  user_id text not null,
  storage_bucket text not null,
  storage_prefix text not null,
  created_at timestamptz not null default clock_timestamp(),
  primary key (session_id_hash, storage_bucket, storage_prefix)
);

insert into public.codex_session_storage_locators(
  session_id_hash,
  user_id,
  storage_bucket,
  storage_prefix
)
select distinct
  encode(
    extensions.digest(
      'codex_rollout_session:' || stored.path_tokens[4],
      'sha256'
    ),
    'hex'
  ),
  stored.path_tokens[2],
  stored.bucket_id,
  array_to_string(stored.path_tokens[1:4], '/')
from storage.objects stored
where cardinality(stored.path_tokens) >= 5
  and stored.path_tokens[1] = 'users'
  and stored.path_tokens[3] = 'sessions'
on conflict do nothing;

insert into public.codex_session_storage_locators(
  session_id_hash,
  user_id,
  storage_bucket,
  storage_prefix
)
select distinct
  encode(
    extensions.digest(
      'codex_rollout_session:' || catalog.session_id,
      'sha256'
    ),
    'hex'
  ),
  catalog.path_tokens[2],
  catalog.storage_bucket,
  array_to_string(catalog.path_tokens[1:4], '/')
from (
  select
    message.session_id,
    message.storage_bucket,
    string_to_array(message.storage_path, '/') as path_tokens
  from public.codex_session_messages message
  union all
  select
    event.session_id,
    event.storage_bucket,
    string_to_array(event.storage_path, '/') as path_tokens
  from public.codex_session_events event
  where event.storage_bucket is not null
    and event.storage_path is not null
) catalog
where cardinality(catalog.path_tokens) >= 5
  and catalog.path_tokens[1] = 'users'
  and catalog.path_tokens[3] = 'sessions'
  and catalog.path_tokens[4] = catalog.session_id
on conflict do nothing;

insert into public.codex_session_storage_locators(
  session_id_hash,
  user_id,
  storage_bucket,
  storage_prefix
)
select
  encode(
    extensions.digest(
      'codex_rollout_session:' || session.id,
      'sha256'
    ),
    'hex'
  ),
  session.user_id::text,
  coalesce(catalog.storage_bucket, 'codex-sessions'),
  session.storage_prefix
from public.codex_sessions session
left join lateral (
  select min(bucket.storage_bucket) as storage_bucket
  from (
    select message.storage_bucket
    from public.codex_session_messages message
    where message.session_id = session.id
    union
    select event.storage_bucket
    from public.codex_session_events event
    where event.session_id = session.id
      and event.storage_bucket is not null
  ) bucket
) catalog on true
where not exists (
  select 1
  from public.codex_session_storage_locators locator
  where locator.session_id_hash = encode(
    extensions.digest(
      'codex_rollout_session:' || session.id,
      'sha256'
    ),
    'hex'
  )
)
on conflict do nothing;

alter table public.codex_ignored_sessions enable row level security;
alter table public.codex_session_storage_locators enable row level security;
revoke all on table public.codex_ignored_sessions
  from public, anon, authenticated, service_role;
revoke all on table public.codex_session_storage_locators
  from public, anon, authenticated, service_role;
grant select on table public.codex_ignored_sessions to service_role;

revoke all privileges
  on public.codex_sessions,
    public.codex_session_messages,
    public.codex_session_events
  from service_role;
grant select, insert, update
  on public.codex_sessions,
    public.codex_session_messages,
    public.codex_session_events
  to service_role;

create or replace function public.codex_session_is_ignored(
  p_session_id text
)
returns boolean
language sql
stable
security definer
set search_path = ''
as $function$
  select exists (
    select 1
    from public.codex_ignored_sessions ignored
    where ignored.session_id_hash = encode(
      extensions.digest(
        'codex_rollout_session:' || p_session_id,
        'sha256'
      ),
      'hex'
    )
  );
$function$;

revoke all on function public.codex_session_is_ignored(text)
  from public, anon, authenticated, service_role;

create or replace function public.reserve_codex_session_storage(
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
  if public.codex_session_is_ignored(p_session_id) then
    return jsonb_build_object('status', 'ignored');
  end if;
  if exists (
    select 1
    from public.codex_session_storage_locators locator
    where locator.session_id_hash = session_hash
      and (
        locator.user_id <> p_user_id
        or locator.storage_bucket <> p_storage_bucket
        or locator.storage_prefix <> p_storage_prefix
      )
  ) then
    raise check_violation using message = 'session storage identity conflicts';
  end if;
  insert into public.codex_session_storage_locators(
    session_id_hash,
    user_id,
    storage_bucket,
    storage_prefix
  )
  values (session_hash, p_user_id, p_storage_bucket, p_storage_prefix)
  on conflict do nothing;
  return jsonb_build_object('status', 'reserved');
end;
$function$;

revoke all on function public.reserve_codex_session_storage(text, text, text, text)
  from public, anon, authenticated;
grant execute on function public.reserve_codex_session_storage(text, text, text, text)
  to service_role;

create or replace function public.fence_codex_session(p_session_id text)
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
begin
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      'codex_rollout_session:' || p_session_id,
      0
    )
  );
  insert into public.codex_ignored_sessions(session_id_hash, ignored_at)
  values (session_hash, pg_catalog.clock_timestamp())
  on conflict (session_id_hash) do nothing;
  return jsonb_build_object('status', 'fenced');
end;
$function$;

revoke all on function public.fence_codex_session(text)
  from public, anon, authenticated;
grant execute on function public.fence_codex_session(text) to service_role;

create or replace function public.reject_ignored_codex_session_write()
returns trigger
language plpgsql
security definer
set search_path = ''
as $function$
declare
  session_id text := coalesce(
    pg_catalog.to_jsonb(new)->>'session_id',
    pg_catalog.to_jsonb(new)->>'id'
  );
  prior_session_id text := coalesce(
    pg_catalog.to_jsonb(old)->>'session_id',
    pg_catalog.to_jsonb(old)->>'id'
  );
  session_hash text := encode(
    extensions.digest(
      'codex_rollout_session:' || session_id,
      'sha256'
    ),
    'hex'
  );
begin
  if tg_op = 'UPDATE' and prior_session_id is distinct from session_id then
    raise check_violation using message = 'session identity is immutable';
  end if;
  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(
      'codex_rollout_session:' || session_id,
      0
    )
  );
  if public.codex_session_is_ignored(session_id) then
    raise check_violation using message = 'session is ignored';
  end if;
  if tg_table_name = 'codex_sessions' and not exists (
    select 1
    from public.codex_session_storage_locators locator
    where locator.session_id_hash = session_hash
      and locator.user_id = pg_catalog.to_jsonb(new)->>'user_id'
      and locator.storage_prefix = pg_catalog.to_jsonb(new)->>'storage_prefix'
  ) then
    raise check_violation using message = 'session storage is not reserved';
  end if;
  if tg_table_name in ('codex_session_messages', 'codex_session_events')
     and not exists (
       select 1
       from public.codex_session_storage_locators locator
       where locator.session_id_hash = session_hash
         and locator.user_id = pg_catalog.to_jsonb(new)->>'user_id'
         and (
           pg_catalog.to_jsonb(new)->>'storage_path' is null
           or (
             locator.storage_bucket = pg_catalog.to_jsonb(new)->>'storage_bucket'
             and pg_catalog.starts_with(
               pg_catalog.to_jsonb(new)->>'storage_path',
               locator.storage_prefix || '/'
             )
           )
         )
     ) then
    raise check_violation using message = 'session object storage is not reserved';
  end if;
  return new;
end;
$function$;

revoke all on function public.reject_ignored_codex_session_write()
  from public, anon, authenticated, service_role;

drop trigger if exists codex_sessions_reject_ignored on public.codex_sessions;
create trigger codex_sessions_reject_ignored
before insert or update on public.codex_sessions
for each row execute function public.reject_ignored_codex_session_write();

drop trigger if exists codex_session_messages_reject_ignored
  on public.codex_session_messages;
create trigger codex_session_messages_reject_ignored
before insert or update on public.codex_session_messages
for each row execute function public.reject_ignored_codex_session_write();

drop trigger if exists codex_session_events_reject_ignored
  on public.codex_session_events;
create trigger codex_session_events_reject_ignored
before insert or update on public.codex_session_events
for each row execute function public.reject_ignored_codex_session_write();

drop trigger if exists codex_session_usage_reject_ignored
  on public.codex_session_usage;
create trigger codex_session_usage_reject_ignored
before insert or update on public.codex_session_usage
for each row execute function public.reject_ignored_codex_session_write();

drop trigger if exists codex_session_usage_observations_reject_ignored
  on public.codex_session_usage_observations;
create trigger codex_session_usage_observations_reject_ignored
before insert or update on public.codex_session_usage_observations
for each row execute function public.reject_ignored_codex_session_write();

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
    select stored.bucket_id, stored.name
    from public.codex_session_storage_locators locator
    join storage.objects stored
      on stored.bucket_id = locator.storage_bucket
      and pg_catalog.starts_with(
        stored.name,
        locator.storage_prefix || '/'
      )
    where locator.session_id_hash = session_hash
    order by stored.bucket_id, stored.name
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
    join storage.objects stored
      on stored.bucket_id = locator.storage_bucket
      and pg_catalog.starts_with(
        stored.name,
        locator.storage_prefix || '/'
      )
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
