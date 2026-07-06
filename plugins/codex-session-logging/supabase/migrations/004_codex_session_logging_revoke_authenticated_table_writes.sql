revoke all privileges on public.codex_sessions from authenticated;
revoke all privileges on public.codex_session_messages from authenticated;
revoke all privileges on public.codex_session_events from authenticated;

grant select on public.codex_sessions to authenticated;
grant select on public.codex_session_messages to authenticated;
grant select on public.codex_session_events to authenticated;
