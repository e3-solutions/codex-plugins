-- Codex agent analytics v2
--
-- Design contract:
--   * Storage owns immutable source bytes.
--   * Postgres owns the queryable projection.
--   * One record table represents messages, tool activity, lifecycle events,
--     reasoning, and usage without discarding the original parsed payload.
--   * A single transaction commits installation, run, and record metadata.

create schema if not exists private;

revoke all on schema private from public;
revoke all on schema private from anon;
revoke all on schema private from authenticated;
grant usage on schema private to authenticated;
grant usage on schema private to service_role;

create table public.workspace_members (
  workspace_id uuid not null,
  user_id uuid not null references auth.users (id) on delete cascade,
  role text not null default 'analyst' check (role in ('owner', 'admin', 'analyst')),
  created_at timestamptz not null default now(),
  primary key (workspace_id, user_id)
);

comment on table public.workspace_members is
  'Data-plane membership used only for tenant-safe reads. Telemetry ingestion uses the service role.';

create table public.agent_installations (
  id uuid primary key,
  workspace_id uuid not null,
  owner_user_id uuid references auth.users (id) on delete set null,
  actor_key text not null check (length(actor_key) between 1 and 512),
  actor_name text,
  actor_email text,
  client_name text not null default 'codex',
  client_version text,
  os_name text,
  architecture text,
  metadata jsonb not null default '{}'::jsonb
    check (jsonb_typeof(metadata) = 'object'),
  first_seen_at timestamptz not null,
  last_seen_at timestamptz not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (workspace_id, id),
  check (last_seen_at >= first_seen_at)
);

comment on table public.agent_installations is
  'Stable Codex installation and human/team identity. One row per installed client.';

create table public.agent_runs (
  id text primary key check (length(id) between 1 and 200),
  workspace_id uuid not null,
  installation_id uuid not null,
  provider text not null default 'codex' check (length(provider) between 1 and 64),
  conversation_id text not null check (length(conversation_id) between 1 and 200),
  parent_run_id text,
  root_run_id text not null check (length(root_run_id) between 1 and 200),
  agent_role text not null default 'root' check (length(agent_role) between 1 and 64),
  depth integer not null default 0 check (depth between 0 and 1024),
  status text not null default 'active' check (length(status) between 1 and 64),
  model text,
  title text,
  origin text not null default 'codex://unscoped',
  repo_url text,
  repo_root text,
  branch text,
  started_at timestamptz not null,
  ended_at timestamptz,
  last_seen_at timestamptz not null,
  metadata jsonb not null default '{}'::jsonb
    check (jsonb_typeof(metadata) = 'object'),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (workspace_id, id),
  foreign key (workspace_id, installation_id)
    references public.agent_installations (workspace_id, id)
    on delete restrict,
  check (parent_run_id is null or parent_run_id <> id),
  check (ended_at is null or ended_at >= started_at),
  check (last_seen_at >= started_at)
);

comment on table public.agent_runs is
  'One Codex session or subagent session. parent_run_id/root_run_id preserve the agent tree even when children arrive first.';
comment on column public.agent_runs.origin is
  'Repository remote when known; codex://unscoped for non-Git and no-origin sessions.';

create table public.agent_records (
  id bigint generated always as identity primary key,
  workspace_id uuid not null,
  run_id text not null,
  source text not null check (length(source) between 1 and 64),
  record_key text not null check (length(record_key) between 1 and 512),
  source_sequence bigint check (source_sequence is null or source_sequence >= 0),
  record_kind text not null check (length(record_kind) between 1 and 128),
  role text,
  turn_id text,
  parent_record_key text,
  tool_name text,
  tool_call_id text,
  model text,
  content_text text,
  occurred_at timestamptz not null,
  input_tokens bigint check (input_tokens is null or input_tokens >= 0),
  output_tokens bigint check (output_tokens is null or output_tokens >= 0),
  cached_input_tokens bigint check (cached_input_tokens is null or cached_input_tokens >= 0),
  reasoning_tokens bigint check (reasoning_tokens is null or reasoning_tokens >= 0),
  total_tokens bigint check (total_tokens is null or total_tokens >= 0),
  cost_usd numeric(20, 8) check (cost_usd is null or cost_usd >= 0),
  usage_scope text,
  usage_is_cumulative boolean,
  payload jsonb not null check (jsonb_typeof(payload) = 'object'),
  raw_bucket text not null,
  raw_path text not null,
  raw_sha256 text not null check (raw_sha256 ~ '^[0-9a-f]{64}$'),
  raw_byte_offset bigint check (raw_byte_offset is null or raw_byte_offset >= 0),
  raw_byte_length bigint check (raw_byte_length is null or raw_byte_length >= 0),
  created_at timestamptz not null default now(),
  foreign key (workspace_id, run_id)
    references public.agent_runs (workspace_id, id)
    on delete cascade,
  unique (run_id, source, record_key)
);

comment on table public.agent_records is
  'Append-only query projection for every message, prompt, tool record, lifecycle event, reasoning item, and usage snapshot.';
comment on column public.agent_records.payload is
  'The complete parsed source record. Convenience columns are projections and never replace this payload or the raw object.';

create table private.session_registry (
  workspace_id uuid not null,
  run_id text not null,
  installation_id uuid not null,
  raw_prefix text not null,
  state text not null default 'active' check (state in ('active', 'deleting', 'deleted')),
  first_ingested_at timestamptz not null default now(),
  last_ingested_at timestamptz not null default now(),
  deleted_at timestamptz,
  deletion_reason text,
  primary key (workspace_id, run_id),
  check ((state = 'deleted') = (deleted_at is not null))
);

comment on table private.session_registry is
  'Private operational tombstone and raw-prefix registry. Deleted sessions cannot be resurrected by retries.';

create index workspace_members_user_workspace_idx
  on public.workspace_members (user_id, workspace_id);

create index agent_installations_workspace_seen_idx
  on public.agent_installations (workspace_id, last_seen_at desc, id);
create index agent_installations_workspace_actor_idx
  on public.agent_installations (workspace_id, actor_key);

create index agent_runs_workspace_started_idx
  on public.agent_runs (workspace_id, started_at desc, id);
create index agent_runs_installation_started_idx
  on public.agent_runs (installation_id, started_at desc, id);
create index agent_runs_parent_idx
  on public.agent_runs (parent_run_id)
  where parent_run_id is not null;
create index agent_runs_root_started_idx
  on public.agent_runs (root_run_id, started_at, id);

create index agent_records_run_time_idx
  on public.agent_records (run_id, occurred_at, id);
create index agent_records_workspace_time_idx
  on public.agent_records (workspace_id, occurred_at desc, id desc);
create index agent_records_message_time_idx
  on public.agent_records (workspace_id, occurred_at desc, id desc)
  where record_kind = 'message';
create index agent_records_prompt_time_idx
  on public.agent_records (workspace_id, occurred_at desc, id desc)
  where record_kind = 'message' and role = 'user';
create index agent_records_usage_time_idx
  on public.agent_records (run_id, occurred_at desc, id desc)
  where record_kind = 'usage';
create index agent_records_tool_time_idx
  on public.agent_records (workspace_id, tool_name, occurred_at desc, id desc)
  where tool_name is not null;
create index agent_records_occurred_brin_idx
  on public.agent_records using brin (occurred_at) with (pages_per_range = 64);

create or replace function private.is_workspace_member(target_workspace_id uuid)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.workspace_members as member
    where member.workspace_id = target_workspace_id
      and member.user_id = (select auth.uid())
  );
$$;

revoke all on function private.is_workspace_member(uuid) from public;
grant execute on function private.is_workspace_member(uuid) to authenticated;
grant execute on function private.is_workspace_member(uuid) to service_role;

create or replace function private.storage_workspace_id(object_name text)
returns uuid
language sql
immutable
set search_path = ''
as $$
  select case
    when split_part(object_name, '/', 1) = 'workspaces'
      and split_part(object_name, '/', 2) ~*
        '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
    then split_part(object_name, '/', 2)::uuid
    else null
  end;
$$;

revoke all on function private.storage_workspace_id(text) from public;
grant execute on function private.storage_workspace_id(text) to authenticated;

alter table public.workspace_members enable row level security;
alter table public.agent_installations enable row level security;
alter table public.agent_runs enable row level security;
alter table public.agent_records enable row level security;

create policy workspace_members_read
on public.workspace_members
for select
to authenticated
using (private.is_workspace_member(workspace_id));

create policy agent_installations_read
on public.agent_installations
for select
to authenticated
using (private.is_workspace_member(workspace_id));

create policy agent_runs_read
on public.agent_runs
for select
to authenticated
using (private.is_workspace_member(workspace_id));

create policy agent_records_read
on public.agent_records
for select
to authenticated
using (private.is_workspace_member(workspace_id));

-- The bucket itself is created through the Storage API. Only authenticated
-- workspace members can read its objects; the Edge Function writes with the
-- service role and therefore bypasses this policy.
create policy agent_rollouts_member_read
on storage.objects
for select
to authenticated
using (
  bucket_id = 'agent-rollouts'
  and private.is_workspace_member(private.storage_workspace_id(name))
);

create or replace function public.commit_agent_batch_v1(
  p_workspace_id uuid,
  p_installation jsonb,
  p_run jsonb,
  p_records jsonb,
  p_raw jsonb
)
returns jsonb
language plpgsql
set search_path = ''
as $$
declare
  v_installation_id uuid;
  v_run_id text;
  v_actor_key text;
  v_started_at timestamptz;
  v_last_seen_at timestamptz;
  v_raw_bucket text;
  v_raw_path text;
  v_raw_sha256 text;
  v_raw_byte_length bigint;
  v_expected_prefix text;
  v_record_count integer;
  v_inserted_count integer;
  v_affected integer;
begin
  if p_workspace_id is null then
    raise exception using errcode = '22023', message = 'workspace_id is required';
  end if;
  if jsonb_typeof(p_installation) is distinct from 'object'
    or jsonb_typeof(p_run) is distinct from 'object'
    or jsonb_typeof(p_records) is distinct from 'array'
    or jsonb_typeof(p_raw) is distinct from 'object'
  then
    raise exception using errcode = '22023', message = 'invalid ingest envelope';
  end if;

  v_installation_id := nullif(p_installation ->> 'id', '')::uuid;
  v_run_id := nullif(p_run ->> 'id', '');
  v_actor_key := nullif(p_installation ->> 'actor_key', '');
  v_started_at := nullif(p_run ->> 'started_at', '')::timestamptz;
  v_last_seen_at := coalesce(
    nullif(p_run ->> 'last_seen_at', '')::timestamptz,
    v_started_at
  );
  v_raw_bucket := nullif(p_raw ->> 'bucket', '');
  v_raw_path := nullif(p_raw ->> 'path', '');
  v_raw_sha256 := nullif(p_raw ->> 'sha256', '');
  v_raw_byte_length := nullif(p_raw ->> 'byte_length', '')::bigint;
  v_record_count := jsonb_array_length(p_records);

  if v_installation_id is null or v_actor_key is null then
    raise exception using errcode = '22023', message = 'installation.id and installation.actor_key are required';
  end if;
  if v_run_id is null or v_run_id !~ '^[A-Za-z0-9._:-]{1,200}$' then
    raise exception using errcode = '22023', message = 'run.id is invalid';
  end if;
  if v_started_at is null or v_last_seen_at < v_started_at then
    raise exception using errcode = '22023', message = 'run timestamps are invalid';
  end if;
  if v_record_count < 1 or v_record_count > 1000 then
    raise exception using errcode = '22023', message = 'records must contain between 1 and 1000 items';
  end if;
  if v_raw_bucket <> 'agent-rollouts'
    or v_raw_path is null
    or v_raw_sha256 !~ '^[0-9a-f]{64}$'
    or v_raw_byte_length is null
    or v_raw_byte_length < 1
  then
    raise exception using errcode = '22023', message = 'raw object metadata is invalid';
  end if;

  v_expected_prefix := format(
    'workspaces/%s/runs/%s/',
    p_workspace_id,
    v_run_id
  );
  if left(v_raw_path, length(v_expected_prefix)) <> v_expected_prefix then
    raise exception using errcode = '22023', message = 'raw object path does not match the workspace and run';
  end if;

  if exists (
    select 1
    from jsonb_array_elements(p_records) as item(value)
    where jsonb_typeof(item.value) is distinct from 'object'
      or nullif(item.value ->> 'source', '') is null
      or nullif(item.value ->> 'record_key', '') is null
      or nullif(item.value ->> 'record_kind', '') is null
      or nullif(item.value ->> 'occurred_at', '') is null
      or jsonb_typeof(item.value -> 'payload') is distinct from 'object'
  ) then
    raise exception using errcode = '22023', message = 'record fields are invalid';
  end if;

  if exists (
    select 1
    from jsonb_array_elements(p_records) as item(value)
    group by item.value ->> 'source', item.value ->> 'record_key'
    having count(*) > 1
  ) then
    raise exception using errcode = '22023', message = 'duplicate record keys in one batch';
  end if;

  perform pg_catalog.pg_advisory_xact_lock(
    pg_catalog.hashtextextended(p_workspace_id::text || ':' || v_run_id, 0)
  );

  if exists (
    select 1
    from private.session_registry as registry
    where registry.workspace_id = p_workspace_id
      and registry.run_id = v_run_id
      and registry.state in ('deleting', 'deleted')
  ) then
    raise exception using errcode = '55000', message = 'run is not accepting ingest';
  end if;

  insert into public.agent_installations (
    id,
    workspace_id,
    owner_user_id,
    actor_key,
    actor_name,
    actor_email,
    client_name,
    client_version,
    os_name,
    architecture,
    metadata,
    first_seen_at,
    last_seen_at
  ) values (
    v_installation_id,
    p_workspace_id,
    nullif(p_installation ->> 'owner_user_id', '')::uuid,
    v_actor_key,
    nullif(p_installation ->> 'actor_name', ''),
    nullif(p_installation ->> 'actor_email', ''),
    coalesce(nullif(p_installation ->> 'client_name', ''), 'codex'),
    nullif(p_installation ->> 'client_version', ''),
    nullif(p_installation ->> 'os_name', ''),
    nullif(p_installation ->> 'architecture', ''),
    coalesce(p_installation -> 'metadata', '{}'::jsonb),
    coalesce(nullif(p_installation ->> 'first_seen_at', '')::timestamptz, v_started_at),
    v_last_seen_at
  )
  on conflict (id) do update
  set
    owner_user_id = coalesce(excluded.owner_user_id, public.agent_installations.owner_user_id),
    actor_key = excluded.actor_key,
    actor_name = coalesce(excluded.actor_name, public.agent_installations.actor_name),
    actor_email = coalesce(excluded.actor_email, public.agent_installations.actor_email),
    client_name = excluded.client_name,
    client_version = coalesce(excluded.client_version, public.agent_installations.client_version),
    os_name = coalesce(excluded.os_name, public.agent_installations.os_name),
    architecture = coalesce(excluded.architecture, public.agent_installations.architecture),
    metadata = public.agent_installations.metadata || excluded.metadata,
    first_seen_at = least(public.agent_installations.first_seen_at, excluded.first_seen_at),
    last_seen_at = greatest(public.agent_installations.last_seen_at, excluded.last_seen_at),
    updated_at = now()
  where public.agent_installations.workspace_id = excluded.workspace_id;

  get diagnostics v_affected = row_count;
  if v_affected <> 1 then
    raise exception using errcode = '23505', message = 'installation belongs to another workspace';
  end if;

  insert into public.agent_runs (
    id,
    workspace_id,
    installation_id,
    provider,
    conversation_id,
    parent_run_id,
    root_run_id,
    agent_role,
    depth,
    status,
    model,
    title,
    origin,
    repo_url,
    repo_root,
    branch,
    started_at,
    ended_at,
    last_seen_at,
    metadata
  ) values (
    v_run_id,
    p_workspace_id,
    v_installation_id,
    coalesce(nullif(p_run ->> 'provider', ''), 'codex'),
    coalesce(nullif(p_run ->> 'conversation_id', ''), v_run_id),
    nullif(p_run ->> 'parent_run_id', ''),
    coalesce(nullif(p_run ->> 'root_run_id', ''), v_run_id),
    coalesce(nullif(p_run ->> 'agent_role', ''), 'root'),
    coalesce(nullif(p_run ->> 'depth', '')::integer, 0),
    coalesce(nullif(p_run ->> 'status', ''), 'active'),
    nullif(p_run ->> 'model', ''),
    nullif(p_run ->> 'title', ''),
    coalesce(nullif(p_run ->> 'origin', ''), 'codex://unscoped'),
    nullif(p_run ->> 'repo_url', ''),
    nullif(p_run ->> 'repo_root', ''),
    nullif(p_run ->> 'branch', ''),
    v_started_at,
    nullif(p_run ->> 'ended_at', '')::timestamptz,
    v_last_seen_at,
    coalesce(p_run -> 'metadata', '{}'::jsonb)
  )
  on conflict (id) do update
  set
    installation_id = excluded.installation_id,
    provider = excluded.provider,
    conversation_id = excluded.conversation_id,
    parent_run_id = coalesce(excluded.parent_run_id, public.agent_runs.parent_run_id),
    root_run_id = excluded.root_run_id,
    agent_role = excluded.agent_role,
    depth = excluded.depth,
    status = excluded.status,
    model = coalesce(excluded.model, public.agent_runs.model),
    title = coalesce(excluded.title, public.agent_runs.title),
    origin = excluded.origin,
    repo_url = coalesce(excluded.repo_url, public.agent_runs.repo_url),
    repo_root = coalesce(excluded.repo_root, public.agent_runs.repo_root),
    branch = coalesce(excluded.branch, public.agent_runs.branch),
    started_at = least(public.agent_runs.started_at, excluded.started_at),
    ended_at = coalesce(excluded.ended_at, public.agent_runs.ended_at),
    last_seen_at = greatest(public.agent_runs.last_seen_at, excluded.last_seen_at),
    metadata = public.agent_runs.metadata || excluded.metadata,
    updated_at = now()
  where public.agent_runs.workspace_id = excluded.workspace_id;

  get diagnostics v_affected = row_count;
  if v_affected <> 1 then
    raise exception using errcode = '23505', message = 'run belongs to another workspace';
  end if;

  insert into private.session_registry (
    workspace_id,
    run_id,
    installation_id,
    raw_prefix,
    last_ingested_at
  ) values (
    p_workspace_id,
    v_run_id,
    v_installation_id,
    v_expected_prefix,
    v_last_seen_at
  )
  on conflict (workspace_id, run_id) do update
  set
    installation_id = excluded.installation_id,
    raw_prefix = excluded.raw_prefix,
    last_ingested_at = greatest(private.session_registry.last_ingested_at, excluded.last_ingested_at);

  if exists (
    select 1
    from jsonb_array_elements(p_records) as item(value)
    join public.agent_records as existing
      on existing.run_id = v_run_id
      and existing.source = item.value ->> 'source'
      and existing.record_key = item.value ->> 'record_key'
    where existing.workspace_id <> p_workspace_id
      or existing.payload <> item.value -> 'payload'
  ) then
    raise exception using errcode = '23505', message = 'record key was reused with different content';
  end if;

  insert into public.agent_records (
    workspace_id,
    run_id,
    source,
    record_key,
    source_sequence,
    record_kind,
    role,
    turn_id,
    parent_record_key,
    tool_name,
    tool_call_id,
    model,
    content_text,
    occurred_at,
    input_tokens,
    output_tokens,
    cached_input_tokens,
    reasoning_tokens,
    total_tokens,
    cost_usd,
    usage_scope,
    usage_is_cumulative,
    payload,
    raw_bucket,
    raw_path,
    raw_sha256,
    raw_byte_offset,
    raw_byte_length
  )
  select
    p_workspace_id,
    v_run_id,
    item.value ->> 'source',
    item.value ->> 'record_key',
    nullif(item.value ->> 'source_sequence', '')::bigint,
    item.value ->> 'record_kind',
    nullif(item.value ->> 'role', ''),
    nullif(item.value ->> 'turn_id', ''),
    nullif(item.value ->> 'parent_record_key', ''),
    nullif(item.value ->> 'tool_name', ''),
    nullif(item.value ->> 'tool_call_id', ''),
    nullif(item.value ->> 'model', ''),
    item.value ->> 'content_text',
    (item.value ->> 'occurred_at')::timestamptz,
    nullif(item.value ->> 'input_tokens', '')::bigint,
    nullif(item.value ->> 'output_tokens', '')::bigint,
    nullif(item.value ->> 'cached_input_tokens', '')::bigint,
    nullif(item.value ->> 'reasoning_tokens', '')::bigint,
    nullif(item.value ->> 'total_tokens', '')::bigint,
    nullif(item.value ->> 'cost_usd', '')::numeric,
    nullif(item.value ->> 'usage_scope', ''),
    nullif(item.value ->> 'usage_is_cumulative', '')::boolean,
    item.value -> 'payload',
    v_raw_bucket,
    v_raw_path,
    v_raw_sha256,
    nullif(item.value ->> 'raw_byte_offset', '')::bigint,
    nullif(item.value ->> 'raw_byte_length', '')::bigint
  from jsonb_array_elements(p_records) as item(value)
  on conflict (run_id, source, record_key) do nothing;

  get diagnostics v_inserted_count = row_count;

  return jsonb_build_object(
    'ok', true,
    'run_id', v_run_id,
    'accepted_records', v_record_count,
    'inserted_records', v_inserted_count,
    'raw_path', v_raw_path
  );
end;
$$;

comment on function public.commit_agent_batch_v1(uuid, jsonb, jsonb, jsonb, jsonb) is
  'Idempotently commits one validated raw batch projection under a per-run transaction lock.';

revoke all on function public.commit_agent_batch_v1(uuid, jsonb, jsonb, jsonb, jsonb) from public;
revoke all on function public.commit_agent_batch_v1(uuid, jsonb, jsonb, jsonb, jsonb) from anon;
revoke all on function public.commit_agent_batch_v1(uuid, jsonb, jsonb, jsonb, jsonb) from authenticated;
grant execute on function public.commit_agent_batch_v1(uuid, jsonb, jsonb, jsonb, jsonb) to service_role;

create view public.activity_export_v1
with (security_invoker = true)
as
select
  record.workspace_id,
  record.id as record_id,
  record.run_id,
  run.conversation_id,
  run.parent_run_id,
  run.root_run_id,
  run.agent_role,
  run.depth,
  run.status as run_status,
  run.provider,
  run.model as run_model,
  run.origin,
  run.repo_url,
  run.repo_root,
  run.branch,
  run.started_at as run_started_at,
  run.ended_at as run_ended_at,
  installation.id as installation_id,
  installation.actor_key,
  installation.actor_name,
  installation.actor_email,
  installation.client_name,
  installation.client_version,
  record.source,
  record.record_key,
  record.source_sequence,
  record.record_kind,
  record.role,
  record.turn_id,
  record.parent_record_key,
  record.tool_name,
  record.tool_call_id,
  record.model,
  record.content_text,
  record.occurred_at,
  record.input_tokens,
  record.output_tokens,
  record.cached_input_tokens,
  record.reasoning_tokens,
  record.total_tokens,
  record.cost_usd,
  record.usage_scope,
  record.usage_is_cumulative,
  record.payload,
  record.raw_bucket,
  record.raw_path,
  record.raw_sha256,
  record.raw_byte_offset,
  record.raw_byte_length
from public.agent_records as record
join public.agent_runs as run
  on run.workspace_id = record.workspace_id
  and run.id = record.run_id
join public.agent_installations as installation
  on installation.workspace_id = run.workspace_id
  and installation.id = run.installation_id;

comment on view public.activity_export_v1 is
  'Versioned, flattened, one-row-per-record export surface for BI, CSV, Parquet, and warehouse egress.';

create view public.latest_usage_v1
with (security_invoker = true)
as
select distinct on (record.workspace_id, record.run_id)
  record.workspace_id,
  record.run_id,
  run.root_run_id,
  run.parent_run_id,
  run.installation_id,
  record.occurred_at,
  record.input_tokens,
  record.output_tokens,
  record.cached_input_tokens,
  record.reasoning_tokens,
  record.total_tokens,
  record.cost_usd,
  record.usage_scope,
  record.usage_is_cumulative
from public.agent_records as record
join public.agent_runs as run
  on run.workspace_id = record.workspace_id
  and run.id = record.run_id
where record.record_kind = 'usage'
order by record.workspace_id, record.run_id, record.occurred_at desc, record.id desc;

comment on view public.latest_usage_v1 is
  'Latest usage snapshot per run, derived rather than maintained as another source-of-truth table.';

create view public.usage_deltas_v1
with (security_invoker = true)
as
with snapshots as (
  select
    record.workspace_id,
    record.run_id,
    record.id as record_id,
    record.occurred_at,
    record.input_tokens,
    record.output_tokens,
    record.cached_input_tokens,
    record.reasoning_tokens,
    record.total_tokens,
    record.cost_usd,
    lag(record.input_tokens) over run_history as previous_input_tokens,
    lag(record.output_tokens) over run_history as previous_output_tokens,
    lag(record.cached_input_tokens) over run_history as previous_cached_input_tokens,
    lag(record.reasoning_tokens) over run_history as previous_reasoning_tokens,
    lag(record.total_tokens) over run_history as previous_total_tokens,
    lag(record.cost_usd) over run_history as previous_cost_usd
  from public.agent_records as record
  where record.record_kind = 'usage'
  window run_history as (
    partition by record.workspace_id, record.run_id
    order by record.occurred_at, record.id
  )
)
select
  snapshots.*,
  case
    when previous_input_tokens is null or input_tokens < previous_input_tokens then input_tokens
    else input_tokens - previous_input_tokens
  end as input_tokens_delta,
  case
    when previous_output_tokens is null or output_tokens < previous_output_tokens then output_tokens
    else output_tokens - previous_output_tokens
  end as output_tokens_delta,
  case
    when previous_cached_input_tokens is null or cached_input_tokens < previous_cached_input_tokens then cached_input_tokens
    else cached_input_tokens - previous_cached_input_tokens
  end as cached_input_tokens_delta,
  case
    when previous_reasoning_tokens is null or reasoning_tokens < previous_reasoning_tokens then reasoning_tokens
    else reasoning_tokens - previous_reasoning_tokens
  end as reasoning_tokens_delta,
  case
    when previous_total_tokens is null or total_tokens < previous_total_tokens then total_tokens
    else total_tokens - previous_total_tokens
  end as total_tokens_delta,
  case
    when previous_cost_usd is null or cost_usd < previous_cost_usd then cost_usd
    else cost_usd - previous_cost_usd
  end as cost_usd_delta,
  (
    (previous_total_tokens is not null and total_tokens < previous_total_tokens)
    or (previous_cost_usd is not null and cost_usd < previous_cost_usd)
  ) as counter_reset
from snapshots;

comment on view public.usage_deltas_v1 is
  'Usage deltas derived from immutable snapshots, with counter-reset detection.';

revoke all on table public.workspace_members from anon, authenticated;
revoke all on table public.agent_installations from anon, authenticated;
revoke all on table public.agent_runs from anon, authenticated;
revoke all on table public.agent_records from anon, authenticated;
revoke all on table public.activity_export_v1 from anon, authenticated;
revoke all on table public.latest_usage_v1 from anon, authenticated;
revoke all on table public.usage_deltas_v1 from anon, authenticated;

grant select on table public.workspace_members to authenticated;
grant select on table public.agent_installations to authenticated;
grant select on table public.agent_runs to authenticated;
grant select on table public.agent_records to authenticated;
grant select on table public.activity_export_v1 to authenticated;
grant select on table public.latest_usage_v1 to authenticated;
grant select on table public.usage_deltas_v1 to authenticated;

grant all on table public.workspace_members to service_role;
grant all on table public.agent_installations to service_role;
grant all on table public.agent_runs to service_role;
grant all on table public.agent_records to service_role;
grant select on table public.activity_export_v1 to service_role;
grant select on table public.latest_usage_v1 to service_role;
grant select on table public.usage_deltas_v1 to service_role;
grant usage, select on sequence public.agent_records_id_seq to service_role;

grant select, insert, update, delete on table private.session_registry to service_role;
