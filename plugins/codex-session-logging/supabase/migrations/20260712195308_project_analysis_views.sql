create index if not exists codex_sessions_repo_user_idx
  on public.codex_sessions (repo, user_id)
  where repo is not null;

create index if not exists ai_session_transcripts_repo_user_idx
  on public.ai_session_transcripts (repo_remote, user_id)
  where repo_remote is not null;

create view public.session_analysis_project_sessions
with (security_invoker = true)
as
with normalized as (
  select
    s.*,
    nullif(
      lower(
        regexp_replace(
          regexp_replace(
            trim(s.repo),
            '^(https?://github\.com/|ssh://git@github\.com/|git@github\.com:)',
            '',
            'i'
          ),
          '(\.git)?/?$',
          '',
          'i'
        )
      ),
      ''
    ) as normalized_project_key
  from public.session_analysis_sessions s
)
select
  coalesce(normalized_project_key, 'unmapped') as project_key,
  case
    when normalized_project_key is null then 'unmapped'
    else regexp_replace(normalized_project_key, '^.*/', '')
  end as project_name,
  case
    when normalized_project_key is null then null
    else 'https://github.com/' || normalized_project_key
  end as canonical_repo_url,
  normalized_project_key is not null as is_project_mapped,
  source,
  platform,
  user_id,
  session_id,
  thread_id,
  transcript_id,
  repo,
  branch,
  cwd,
  started_at,
  ended_at,
  has_live_session,
  metadata
from normalized;

create view public.session_analysis_projects
with (security_invoker = true)
as
select
  project_key,
  project_name,
  canonical_repo_url,
  is_project_mapped,
  count(*) as session_count,
  count(distinct user_id) as user_count,
  count(*) filter (where source = 'live') as live_session_count,
  count(*) filter (where source = 'archive') as archive_session_count,
  count(*) filter (where platform = 'codex') as codex_session_count,
  count(*) filter (where platform = 'claude') as claude_session_count,
  min(started_at) as first_activity_at,
  max(coalesce(ended_at, started_at)) as last_activity_at
from public.session_analysis_project_sessions
group by project_key, project_name, canonical_repo_url, is_project_mapped;

create view public.session_analysis_project_messages
with (security_invoker = true)
as
select
  p.project_key,
  p.project_name,
  p.canonical_repo_url,
  p.is_project_mapped,
  m.*
from public.session_analysis_project_sessions p
join public.session_analysis_messages m
  on m.source = p.source
 and m.user_id = p.user_id
 and (
   (m.source = 'live' and m.session_id = p.session_id)
   or (m.source = 'archive' and m.transcript_id = p.transcript_id)
 );

create view public.session_analysis_project_tool_events
with (security_invoker = true)
as
select
  p.project_key,
  p.project_name,
  p.canonical_repo_url,
  p.is_project_mapped,
  e.*
from public.session_analysis_project_sessions p
join public.session_analysis_tool_events e
  on e.source = p.source
 and e.user_id = p.user_id
 and (
   (e.source = 'live' and e.session_id = p.session_id)
   or (e.source = 'archive' and e.transcript_id = p.transcript_id)
 );

create view public.session_analysis_project_usage
with (security_invoker = true)
as
select
  p.project_key,
  p.project_name,
  p.canonical_repo_url,
  p.is_project_mapped,
  u.*
from public.session_analysis_project_sessions p
join public.session_analysis_usage u
  on u.source = p.source
 and u.user_id = p.user_id
 and (
   (u.source = 'live' and u.session_id = p.session_id)
   or (u.source = 'archive' and u.transcript_id = p.transcript_id)
 );

create view public.session_analysis_project_models
with (security_invoker = true)
as
select
  p.project_key,
  p.project_name,
  p.canonical_repo_url,
  p.is_project_mapped,
  m.*
from public.session_analysis_project_sessions p
join public.session_analysis_models m
  on m.source = p.source
 and m.user_id = p.user_id
 and (
   (m.source = 'live' and m.session_id = p.session_id)
   or (m.source = 'archive' and m.transcript_id = p.transcript_id)
 );

revoke all privileges on public.session_analysis_project_sessions from anon, authenticated, service_role;
revoke all privileges on public.session_analysis_projects from anon, authenticated, service_role;
revoke all privileges on public.session_analysis_project_messages from anon, authenticated, service_role;
revoke all privileges on public.session_analysis_project_tool_events from anon, authenticated, service_role;
revoke all privileges on public.session_analysis_project_usage from anon, authenticated, service_role;
revoke all privileges on public.session_analysis_project_models from anon, authenticated, service_role;

grant select on public.session_analysis_project_sessions to authenticated, service_role;
grant select on public.session_analysis_projects to authenticated, service_role;
grant select on public.session_analysis_project_messages to authenticated, service_role;
grant select on public.session_analysis_project_tool_events to authenticated, service_role;
grant select on public.session_analysis_project_usage to authenticated, service_role;
grant select on public.session_analysis_project_models to authenticated, service_role;
