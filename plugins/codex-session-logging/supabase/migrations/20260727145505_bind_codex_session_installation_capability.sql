alter table public.codex_sessions
  add column installation_capability_sha256 text;

alter table public.codex_sessions
  add constraint codex_sessions_installation_capability_sha256_check
  check (
    installation_capability_sha256 is null
    or installation_capability_sha256 ~ '^[0-9a-f]{64}$'
  );

update public.codex_sessions
set installation_capability_sha256 = pg_catalog.encode(
  pg_catalog.sha256(
    pg_catalog.convert_to(
      metadata #>> '{client,installation_id}',
      'UTF8'
    )
  ),
  'hex'
)
where metadata #>> '{client,installation_id}'
  ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$';

create or replace function public.preserve_codex_session_binding()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
  installation_id text;
begin
  if tg_op = 'INSERT' then
    installation_id := new.metadata #>> '{client,installation_id}';
    if installation_id
      ~ '^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
    then
      new.installation_capability_sha256 = pg_catalog.encode(
        pg_catalog.sha256(
          pg_catalog.convert_to(installation_id, 'UTF8')
        ),
        'hex'
      );
    else
      new.installation_capability_sha256 = null;
    end if;
    return new;
  end if;

  new.user_id = old.user_id;
  new.installation_capability_sha256 =
    old.installation_capability_sha256;
  return new;
end;
$$;

revoke execute on function public.preserve_codex_session_binding()
from public;

create trigger preserve_codex_session_binding
before insert or update on public.codex_sessions
for each row
execute function public.preserve_codex_session_binding();
