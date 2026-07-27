create or replace function public.fill_codex_session_thread_id()
returns trigger
language plpgsql
set search_path = pg_catalog, extensions
as $$
begin
  if new.thread_id is null then
    new.thread_id = encode(extensions.digest(new.id, 'sha256'), 'hex');
  end if;
  return new;
end;
$$;
