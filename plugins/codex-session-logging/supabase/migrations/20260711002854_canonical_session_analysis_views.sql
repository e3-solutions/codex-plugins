create view public.session_analysis_sessions
with (security_invoker = true)
as
select
  'live'::text as source,
  case
    when lower(coalesce(s.metadata->>'platform', '')) in ('claude', 'claude-code') then 'claude'
    else 'codex'
  end as platform,
  s.user_id,
  s.id as session_id,
  s.thread_id,
  null::uuid as transcript_id,
  s.repo,
  s.branch,
  null::text as cwd,
  s.started_at,
  s.ended_at,
  false as has_live_session,
  s.metadata
from public.codex_sessions s
union all
select
  'archive'::text,
  t.platform,
  t.user_id,
  t.session_id,
  null::text,
  t.id,
  t.repo_remote,
  null::text,
  t.cwd,
  t.started_at,
  t.ended_at,
  exists (
    select 1
    from public.codex_sessions live
    where live.id = t.session_id
      and live.user_id = t.user_id
  ),
  t.metadata || jsonb_build_object(
    'import_id', t.import_id,
    'source_path', t.source_path,
    'verification', t.verification
  )
from public.ai_session_transcripts t;

create view public.session_analysis_messages
with (security_invoker = true)
as
select
  'live'::text as source,
  case
    when lower(coalesce(m.metadata->>'platform', '')) in ('claude', 'claude-code') then 'claude'
    else 'codex'
  end as platform,
  m.user_id,
  m.session_id,
  null::uuid as transcript_id,
  m.id::text as record_id,
  m.seq,
  m.created_at as occurred_at,
  m.role,
  m.content_excerpt as content,
  null::jsonb as payload,
  true as is_canonical,
  false as has_live_session,
  m.metadata
from public.codex_session_messages m
union all
select
  'archive'::text,
  r.platform,
  r.user_id,
  r.session_id,
  r.transcript_id,
  r.id::text,
  r.seq,
  r.occurred_at,
  r.role,
  r.content_excerpt,
  r.payload,
  case
    when r.platform = 'claude' then true
    when r.record_type = 'event_msg' and r.record_subtype = 'user_message' then true
    when r.record_type = 'response_item'
      and r.record_subtype = 'message'
      and coalesce(r.role, '') <> 'user' then true
    else false
  end,
  exists (
    select 1
    from public.codex_sessions live
    where live.id = r.session_id
      and live.user_id = r.user_id
  ),
  jsonb_build_object(
    'import_id', r.import_id,
    'record_type', r.record_type,
    'record_subtype', r.record_subtype
  )
from public.ai_session_records r
where (
    r.platform = 'codex'
    and (
      (r.record_type = 'event_msg' and r.record_subtype in ('user_message', 'agent_message'))
      or (r.record_type = 'response_item' and r.record_subtype = 'message')
    )
  )
  or (r.platform = 'claude' and r.record_type in ('user', 'assistant'));

create view public.session_analysis_tool_events
with (security_invoker = true)
as
select
  'live'::text as source,
  case
    when lower(coalesce(e.metadata->>'platform', '')) in ('claude', 'claude-code') then 'claude'
    else 'codex'
  end as platform,
  e.user_id,
  e.session_id,
  null::uuid as transcript_id,
  e.id::text as record_id,
  e.seq,
  e.created_at as occurred_at,
  e.event_type,
  case
    when e.event_type like '%finished' or e.event_type like '%result' then 'result'
    when e.event_type like '%failed' then 'failed'
    else 'call'
  end as phase,
  e.metadata->>'tool_name' as tool_name,
  coalesce(e.metadata->>'tool_call_id', e.metadata->>'tool_use_id') as tool_call_id,
  null::jsonb as input,
  null::jsonb as output,
  e.metadata as payload,
  false as has_live_session
from public.codex_session_events e
where e.event_type like 'tool_%'
   or e.metadata ? 'tool_name'
union all
select
  'archive'::text,
  'codex'::text,
  r.user_id,
  r.session_id,
  r.transcript_id,
  r.id::text,
  r.seq,
  r.occurred_at,
  coalesce(r.record_subtype, r.record_type),
  case
    when coalesce(r.record_subtype, '') like '%output'
      or coalesce(r.record_subtype, '') like '%result' then 'result'
    when coalesce(r.record_subtype, '') like '%failed' then 'failed'
    else 'call'
  end,
  r.tool_name,
  coalesce(r.payload#>>'{payload,call_id}', r.payload#>>'{payload,id}'),
  coalesce(r.payload#>'{payload,input}', r.payload#>'{payload,arguments}'),
  coalesce(r.payload#>'{payload,output}', r.payload#>'{payload,result}'),
  r.payload,
  exists (
    select 1 from public.codex_sessions live
    where live.id = r.session_id and live.user_id = r.user_id
  )
from public.ai_session_records r
where r.platform = 'codex'
  and (
    r.tool_name is not null
    or r.record_subtype in (
      'function_call_output', 'custom_tool_call_output', 'tool_search_output'
    )
  )
union all
select
  'archive'::text,
  'claude'::text,
  r.user_id,
  r.session_id,
  r.transcript_id,
  r.id::text || ':' || block.ordinality::text,
  r.seq,
  r.occurred_at,
  block.value->>'type',
  case when block.value->>'type' = 'tool_result' then 'result' else 'call' end,
  block.value->>'name',
  coalesce(block.value->>'id', block.value->>'tool_use_id'),
  block.value->'input',
  block.value->'content',
  r.payload,
  exists (
    select 1 from public.codex_sessions live
    where live.id = r.session_id and live.user_id = r.user_id
  )
from public.ai_session_records r
cross join lateral jsonb_array_elements(
  case
    when jsonb_typeof(r.payload#>'{message,content}') = 'array'
      then r.payload#>'{message,content}'
    else '[]'::jsonb
  end
) with ordinality as block(value, ordinality)
where r.platform = 'claude'
  and block.value->>'type' in ('tool_use', 'tool_result');

create view public.session_analysis_usage
with (security_invoker = true)
as
with codex_archive_ranked as (
  select
    r.*,
    r.payload#>'{payload,info,total_token_usage}' as usage,
    row_number() over (
      partition by r.transcript_id
      order by r.occurred_at desc nulls last, r.seq desc
    ) as usage_rank
  from public.ai_session_records r
  where r.platform = 'codex'
    and r.record_type = 'event_msg'
    and r.record_subtype = 'token_count'
    and jsonb_typeof(r.payload#>'{payload,info,total_token_usage}') = 'object'
), claude_archive_usage as (
  select
    r.user_id,
    r.session_id,
    r.transcript_id,
    max(r.occurred_at) as observed_at,
    sum(case when r.payload#>>'{message,usage,input_tokens}' ~ '^[0-9]+$'
      then (r.payload#>>'{message,usage,input_tokens}')::bigint else 0 end) as input_tokens,
    sum(case when r.payload#>>'{message,usage,cache_read_input_tokens}' ~ '^[0-9]+$'
      then (r.payload#>>'{message,usage,cache_read_input_tokens}')::bigint else 0 end) as cached_input_tokens,
    sum(case when r.payload#>>'{message,usage,cache_creation_input_tokens}' ~ '^[0-9]+$'
      then (r.payload#>>'{message,usage,cache_creation_input_tokens}')::bigint else 0 end) as cache_creation_input_tokens,
    sum(case when r.payload#>>'{message,usage,output_tokens}' ~ '^[0-9]+$'
      then (r.payload#>>'{message,usage,output_tokens}')::bigint else 0 end) as output_tokens,
    count(*) as usage_event_count
  from public.ai_session_records r
  where r.platform = 'claude'
    and jsonb_typeof(r.payload#>'{message,usage}') = 'object'
  group by r.user_id, r.session_id, r.transcript_id
)
select
  'live'::text as source,
  'codex'::text as platform,
  u.user_id,
  u.session_id,
  null::uuid as transcript_id,
  u.observed_at,
  u.input_tokens,
  u.cached_input_tokens,
  0::bigint as cache_creation_input_tokens,
  u.output_tokens,
  u.reasoning_output_tokens,
  u.total_tokens,
  u.model_context_window,
  1::bigint as usage_event_count,
  u.metadata
from public.codex_session_usage u
union all
select
  'archive'::text,
  'codex'::text,
  r.user_id,
  r.session_id,
  r.transcript_id,
  r.occurred_at,
  case when r.usage->>'input_tokens' ~ '^[0-9]+$' then (r.usage->>'input_tokens')::bigint else 0 end,
  case when r.usage->>'cached_input_tokens' ~ '^[0-9]+$' then (r.usage->>'cached_input_tokens')::bigint else 0 end,
  0::bigint,
  case when r.usage->>'output_tokens' ~ '^[0-9]+$' then (r.usage->>'output_tokens')::bigint else 0 end,
  case when r.usage->>'reasoning_output_tokens' ~ '^[0-9]+$' then (r.usage->>'reasoning_output_tokens')::bigint else 0 end,
  case when r.usage->>'total_tokens' ~ '^[0-9]+$' then (r.usage->>'total_tokens')::bigint else 0 end,
  case when r.payload#>>'{payload,info,model_context_window}' ~ '^[0-9]+$'
    then (r.payload#>>'{payload,info,model_context_window}')::bigint else null end,
  1::bigint,
  jsonb_build_object('import_id', r.import_id, 'record_id', r.id)
from codex_archive_ranked r
where r.usage_rank = 1
  and not exists (
    select 1 from public.codex_session_usage live
    where live.user_id = r.user_id and live.session_id = r.session_id
  )
union all
select
  'archive'::text,
  'claude'::text,
  c.user_id,
  c.session_id,
  c.transcript_id,
  c.observed_at,
  c.input_tokens,
  c.cached_input_tokens,
  c.cache_creation_input_tokens,
  c.output_tokens,
  0::bigint,
  c.input_tokens + c.cached_input_tokens + c.cache_creation_input_tokens + c.output_tokens,
  null::bigint,
  c.usage_event_count,
  '{}'::jsonb
from claude_archive_usage c;

create view public.session_analysis_models
with (security_invoker = true)
as
with model_events as (
  select
    'live'::text as source,
    case
      when lower(coalesce(s.metadata->>'platform', '')) in ('claude', 'claude-code') then 'claude'
      else 'codex'
    end as platform,
    s.user_id,
    s.id as session_id,
    null::uuid as transcript_id,
    s.started_at as observed_at,
    nullif(s.metadata->>'model', '') as model
  from public.codex_sessions s
  where nullif(s.metadata->>'model', '') is not null
  union all
  select
    'archive', 'codex', r.user_id, r.session_id, r.transcript_id, r.occurred_at,
    nullif(r.payload#>>'{payload,model}', '')
  from public.ai_session_records r
  where r.platform = 'codex'
    and r.record_type = 'turn_context'
    and nullif(r.payload#>>'{payload,model}', '') is not null
  union all
  select
    'archive', 'claude', r.user_id, r.session_id, r.transcript_id, r.occurred_at,
    nullif(r.payload#>>'{message,model}', '')
  from public.ai_session_records r
  where r.platform = 'claude'
    and r.record_type = 'assistant'
    and nullif(r.payload#>>'{message,model}', '') is not null
)
select
  source,
  platform,
  user_id,
  session_id,
  transcript_id,
  model,
  min(observed_at) as first_observed_at,
  max(observed_at) as last_observed_at,
  count(*) as event_count
from model_events
group by source, platform, user_id, session_id, transcript_id, model;

revoke all privileges on public.session_analysis_sessions from anon, authenticated, service_role;
revoke all privileges on public.session_analysis_messages from anon, authenticated, service_role;
revoke all privileges on public.session_analysis_tool_events from anon, authenticated, service_role;
revoke all privileges on public.session_analysis_usage from anon, authenticated, service_role;
revoke all privileges on public.session_analysis_models from anon, authenticated, service_role;

grant select on public.session_analysis_sessions to authenticated, service_role;
grant select on public.session_analysis_messages to authenticated, service_role;
grant select on public.session_analysis_tool_events to authenticated, service_role;
grant select on public.session_analysis_usage to authenticated, service_role;
grant select on public.session_analysis_models to authenticated, service_role;
