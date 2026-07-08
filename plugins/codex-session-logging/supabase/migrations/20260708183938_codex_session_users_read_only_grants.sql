revoke all privileges on public.codex_session_users from anon, authenticated;
revoke all privileges on public.codex_session_users from service_role;

grant select on public.codex_session_users to authenticated;
grant select, insert, update on public.codex_session_users to service_role;
