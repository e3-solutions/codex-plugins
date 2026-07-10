create table public.codex_session_usage (
  session_id text primary key references public.codex_sessions(id) on delete cascade,
  user_id uuid not null,
  input_tokens bigint not null check (input_tokens >= 0),
  cached_input_tokens bigint not null default 0 check (cached_input_tokens >= 0),
  output_tokens bigint not null check (output_tokens >= 0),
  reasoning_output_tokens bigint not null default 0 check (reasoning_output_tokens >= 0),
  total_tokens bigint not null check (total_tokens >= 0),
  model_context_window bigint check (model_context_window is null or model_context_window >= 0),
  observed_at timestamptz not null,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index codex_session_usage_user_observed_idx
  on public.codex_session_usage (user_id, observed_at desc);

alter table public.codex_session_usage enable row level security;

create policy "Users can read own Codex session usage"
  on public.codex_session_usage
  for select
  to authenticated
  using ((select auth.uid()) = user_id);

revoke all privileges on public.codex_session_usage from anon, authenticated;
revoke all privileges on public.codex_session_usage from service_role;

grant select on public.codex_session_usage to authenticated;
grant select, insert, update on public.codex_session_usage to service_role;
