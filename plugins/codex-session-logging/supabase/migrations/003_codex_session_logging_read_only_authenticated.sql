revoke all privileges on public.codex_sessions from authenticated;
revoke all privileges on public.codex_session_messages from authenticated;
revoke all privileges on public.codex_session_events from authenticated;

grant select on public.codex_sessions to authenticated;
grant select on public.codex_session_messages to authenticated;
grant select on public.codex_session_events to authenticated;

drop policy if exists "Users can insert own Codex sessions" on public.codex_sessions;
drop policy if exists "Users can update own Codex sessions" on public.codex_sessions;
drop policy if exists "Users can insert own Codex session messages" on public.codex_session_messages;
drop policy if exists "Users can insert own Codex session events" on public.codex_session_events;
drop policy if exists "Users can upload own Codex session objects" on storage.objects;
drop policy if exists "Users can update own Codex session objects" on storage.objects;
