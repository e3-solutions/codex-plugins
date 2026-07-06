grant usage on schema public to authenticated, service_role;
grant select, insert, update on public.codex_sessions to authenticated, service_role;
grant select, insert on public.codex_session_messages to authenticated;
grant select, insert, update on public.codex_session_messages to service_role;
grant select, insert on public.codex_session_events to authenticated, service_role;
