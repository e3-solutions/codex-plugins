create table public.ai_session_imports (
  id uuid primary key,
  user_id uuid not null,
  archive_sha256 text not null,
  source_filename text not null,
  redacted boolean not null,
  status text not null check (status in ('importing', 'complete', 'failed')),
  transcript_count integer not null default 0 check (transcript_count >= 0),
  record_count bigint not null default 0 check (record_count >= 0),
  parsed_record_count bigint not null default 0 check (parsed_record_count >= 0),
  invalid_record_count bigint not null default 0 check (invalid_record_count >= 0),
  manifest jsonb not null default '{}'::jsonb,
  metadata jsonb not null default '{}'::jsonb,
  started_at timestamptz not null default now(),
  completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, archive_sha256)
);

create table public.ai_session_transcripts (
  id uuid primary key,
  import_id uuid not null references public.ai_session_imports(id) on delete cascade,
  user_id uuid not null,
  platform text not null check (platform in ('codex', 'claude')),
  session_id text,
  repo_remote text,
  cwd text,
  source_path text not null,
  verification text,
  byte_size bigint not null default 0 check (byte_size >= 0),
  record_count integer not null default 0 check (record_count >= 0),
  parsed_record_count integer not null default 0 check (parsed_record_count >= 0),
  invalid_record_count integer not null default 0 check (invalid_record_count >= 0),
  started_at timestamptz,
  ended_at timestamptz,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (import_id, source_path)
);

create table public.ai_session_records (
  id uuid primary key,
  import_id uuid not null references public.ai_session_imports(id) on delete cascade,
  transcript_id uuid not null references public.ai_session_transcripts(id) on delete cascade,
  user_id uuid not null,
  platform text not null check (platform in ('codex', 'claude')),
  session_id text,
  seq integer not null check (seq > 0),
  record_type text not null,
  record_subtype text,
  occurred_at timestamptz,
  role text,
  tool_name text,
  content_excerpt text,
  payload jsonb,
  raw_text text,
  parse_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check ((payload is not null) <> (raw_text is not null)),
  unique (transcript_id, seq)
);

create index ai_session_imports_user_created_idx
  on public.ai_session_imports (user_id, created_at desc);

create index ai_session_transcripts_user_platform_started_idx
  on public.ai_session_transcripts (user_id, platform, started_at desc);

create index ai_session_transcripts_user_session_idx
  on public.ai_session_transcripts (user_id, session_id);

create index ai_session_records_transcript_seq_idx
  on public.ai_session_records (transcript_id, seq);

create index ai_session_records_import_idx
  on public.ai_session_records (import_id);

create index ai_session_records_user_occurred_idx
  on public.ai_session_records (user_id, occurred_at desc);

create index ai_session_records_user_type_idx
  on public.ai_session_records (user_id, platform, record_type, record_subtype);

create index ai_session_records_user_tool_idx
  on public.ai_session_records (user_id, tool_name)
  where tool_name is not null;

alter table public.ai_session_imports enable row level security;
alter table public.ai_session_transcripts enable row level security;
alter table public.ai_session_records enable row level security;

create policy "Users can read own AI session imports"
  on public.ai_session_imports
  for select
  to authenticated
  using ((select auth.uid()) = user_id);

create policy "Users can read own AI session transcripts"
  on public.ai_session_transcripts
  for select
  to authenticated
  using ((select auth.uid()) = user_id);

create policy "Users can read own AI session records"
  on public.ai_session_records
  for select
  to authenticated
  using ((select auth.uid()) = user_id);

revoke all privileges on public.ai_session_imports from anon, authenticated, service_role;
revoke all privileges on public.ai_session_transcripts from anon, authenticated, service_role;
revoke all privileges on public.ai_session_records from anon, authenticated, service_role;

grant select on public.ai_session_imports to authenticated;
grant select on public.ai_session_transcripts to authenticated;
grant select on public.ai_session_records to authenticated;

grant select, insert, update, delete on public.ai_session_imports to service_role;
grant select, insert, update, delete on public.ai_session_transcripts to service_role;
grant select, insert, update, delete on public.ai_session_records to service_role;
