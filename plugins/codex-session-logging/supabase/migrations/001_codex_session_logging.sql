create extension if not exists pgcrypto;

create table if not exists public.codex_sessions (
  id text primary key,
  user_id uuid not null,
  repo text,
  branch text,
  storage_prefix text not null,
  metadata jsonb not null default '{}'::jsonb,
  started_at timestamptz not null default now(),
  ended_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.codex_session_messages (
  id uuid primary key default gen_random_uuid(),
  session_id text not null references public.codex_sessions(id) on delete cascade,
  user_id uuid not null,
  turn_id text,
  seq integer not null,
  role text not null check (role in ('user', 'assistant')),
  storage_bucket text not null default 'codex-sessions',
  storage_path text not null,
  content_sha256 text not null,
  content_byte_size integer not null check (content_byte_size >= 0),
  content_excerpt text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (session_id, seq)
);

create table if not exists public.codex_session_events (
  id uuid primary key default gen_random_uuid(),
  session_id text not null references public.codex_sessions(id) on delete cascade,
  user_id uuid not null,
  seq integer not null,
  event_type text not null,
  storage_bucket text,
  storage_path text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists codex_sessions_user_created_idx
  on public.codex_sessions (user_id, created_at desc);

create index if not exists codex_session_messages_user_session_seq_idx
  on public.codex_session_messages (user_id, session_id, seq);

create index if not exists codex_session_events_user_session_seq_idx
  on public.codex_session_events (user_id, session_id, seq);

alter table public.codex_sessions enable row level security;
alter table public.codex_session_messages enable row level security;
alter table public.codex_session_events enable row level security;

create policy "Users can read own Codex sessions"
  on public.codex_sessions
  for select
  to authenticated
  using ((select auth.uid()) = user_id);

create policy "Users can insert own Codex sessions"
  on public.codex_sessions
  for insert
  to authenticated
  with check ((select auth.uid()) = user_id);

create policy "Users can update own Codex sessions"
  on public.codex_sessions
  for update
  to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

create policy "Users can read own Codex session messages"
  on public.codex_session_messages
  for select
  to authenticated
  using ((select auth.uid()) = user_id);

create policy "Users can insert own Codex session messages"
  on public.codex_session_messages
  for insert
  to authenticated
  with check ((select auth.uid()) = user_id);

create policy "Users can read own Codex session events"
  on public.codex_session_events
  for select
  to authenticated
  using ((select auth.uid()) = user_id);

create policy "Users can insert own Codex session events"
  on public.codex_session_events
  for insert
  to authenticated
  with check ((select auth.uid()) = user_id);

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'codex-sessions',
  'codex-sessions',
  false,
  null,
  array['application/json', 'application/x-ndjson', 'application/gzip']::text[]
)
on conflict (id) do update
set
  public = excluded.public,
  file_size_limit = excluded.file_size_limit,
  allowed_mime_types = excluded.allowed_mime_types;

create policy "Users can read own Codex session objects"
  on storage.objects
  for select
  to authenticated
  using (
    bucket_id = 'codex-sessions'
    and (storage.foldername(name))[1] = 'users'
    and (storage.foldername(name))[2] = (select auth.uid())::text
  );

create policy "Users can upload own Codex session objects"
  on storage.objects
  for insert
  to authenticated
  with check (
    bucket_id = 'codex-sessions'
    and (storage.foldername(name))[1] = 'users'
    and (storage.foldername(name))[2] = (select auth.uid())::text
  );

create policy "Users can update own Codex session objects"
  on storage.objects
  for update
  to authenticated
  using (
    bucket_id = 'codex-sessions'
    and (storage.foldername(name))[1] = 'users'
    and (storage.foldername(name))[2] = (select auth.uid())::text
  )
  with check (
    bucket_id = 'codex-sessions'
    and (storage.foldername(name))[1] = 'users'
    and (storage.foldername(name))[2] = (select auth.uid())::text
  );
