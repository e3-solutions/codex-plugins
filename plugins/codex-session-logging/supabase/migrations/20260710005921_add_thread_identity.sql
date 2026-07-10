alter table public.codex_sessions
  add column if not exists thread_id text;

with session_transcripts as (
  select distinct on (session_id)
    session_id,
    transcript_path
  from (
    select session_id, created_at, metadata ->> 'transcript_path' as transcript_path
    from public.codex_session_events
    union all
    select session_id, created_at, metadata ->> 'transcript_path' as transcript_path
    from public.codex_session_messages
  ) records
  where nullif(transcript_path, '') is not null
  order by session_id, created_at desc
)
update public.codex_sessions
set thread_id = encode(digest(session_transcripts.transcript_path, 'sha256'), 'hex')
from session_transcripts
where public.codex_sessions.id = session_transcripts.session_id
  and public.codex_sessions.thread_id is null;

update public.codex_sessions
set thread_id = encode(digest(id, 'sha256'), 'hex')
where thread_id is null;

alter table public.codex_sessions
  alter column thread_id set not null;

create index if not exists codex_sessions_user_thread_created_idx
  on public.codex_sessions (user_id, thread_id, created_at desc);
