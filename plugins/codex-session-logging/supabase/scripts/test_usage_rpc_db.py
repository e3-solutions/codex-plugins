#!/usr/bin/env python3
"""Apply and exercise the usage RPC migration against a disposable local database."""

from __future__ import annotations

import hashlib
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb


DATABASE_URL_ENV = "CODEX_SESSION_LOG_TEST_DATABASE_URL"
USAGE_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "20260727053107_upsert_codex_session_usage_latest.sql"
)
CAPABILITY_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "20260727145505_bind_codex_session_installation_capability.sql"
)
CAPABILITY_ACL_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "20260727153053_revoke_codex_session_binding_execute.sql"
)
REPAIR_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "20260727193553_repair_additive_session_usage.sql"
)
RPC = """
select public.upsert_codex_session_usage_latest(
  %s, %s, %s, %s, %s, %s, %s, %s, %s
)
"""


def invoke(database_url: str, session_id: str, total: int, observed_at: str) -> None:
    with psycopg.connect(database_url, autocommit=True) as connection:
        connection.execute("set role service_role")
        connection.execute(
            RPC,
            (
                session_id,
                total - 20,
                10,
                8,
                2,
                total,
                258400,
                observed_at,
                Jsonb({"source": "db_integration_test"}),
            ),
        )


def main() -> None:
    base_url = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not base_url:
        raise SystemExit(f"{DATABASE_URL_ENV} is required")

    parameters = conninfo_to_dict(base_url)
    admin_parameters = {**parameters, "dbname": "postgres"}
    database_name = f"codex_usage_test_{uuid.uuid4().hex}"
    test_parameters = {**parameters, "dbname": database_name}
    test_url = psycopg.conninfo.make_conninfo(**test_parameters)

    with psycopg.connect(**admin_parameters, autocommit=True) as admin:
        missing_roles = admin.execute(
            """
            select role_name
            from unnest(array['anon', 'authenticated', 'service_role'])
              as roles(role_name)
            where not exists (select from pg_roles where rolname = role_name)
            """
        ).fetchall()
        if missing_roles:
            raise SystemExit("local Supabase roles are missing")
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(database_name)))

    try:
        bound_session = str(uuid.uuid4())
        unbound_session = str(uuid.uuid4())
        bound_owner = uuid.uuid4()
        unbound_owner = uuid.uuid4()
        installation_id = str(uuid.uuid4())
        expected_capability = hashlib.sha256(installation_id.encode()).hexdigest()
        inserted_without_digest = str(uuid.uuid4())
        inserted_with_wrong_digest = str(uuid.uuid4())
        inserted_owner = uuid.uuid4()
        inserted_installation_id = str(uuid.uuid4())
        inserted_capability = hashlib.sha256(
            inserted_installation_id.encode()
        ).hexdigest()
        historical_repair = str(uuid.uuid4())
        transcript_repair = str(uuid.uuid4())
        transcript_other_agent = str(uuid.uuid4())
        transcript_oversized_cache = str(uuid.uuid4())
        unknown_source = str(uuid.uuid4())
        historical_near_miss = str(uuid.uuid4())
        repair_owner = uuid.uuid4()
        with psycopg.connect(test_url, autocommit=True) as connection:
            connection.execute(
                """
                create table public.codex_sessions (
                  id text primary key,
                  user_id uuid not null,
                  metadata jsonb not null default '{}'::jsonb
                );
                create table public.codex_session_usage (
                  session_id text primary key references public.codex_sessions(id),
                  user_id uuid not null,
                  input_tokens bigint not null check (input_tokens >= 0),
                  cached_input_tokens bigint not null check (cached_input_tokens >= 0),
                  output_tokens bigint not null check (output_tokens >= 0),
                  reasoning_output_tokens bigint not null check (reasoning_output_tokens >= 0),
                  total_tokens bigint not null check (total_tokens >= 0),
                  model_context_window bigint,
                  observed_at timestamptz not null,
                  metadata jsonb not null default '{}'::jsonb,
                  created_at timestamptz not null default now(),
                  updated_at timestamptz not null default now()
                );
                grant usage on schema public to service_role;
                grant select on public.codex_sessions to service_role;
                grant select, insert, update on public.codex_session_usage to service_role;
                """
            )
            connection.execute(
                """
                insert into public.codex_sessions (id, user_id, metadata)
                values
                  (%s, %s, %s),
                  (%s, %s, %s)
                """,
                (
                    bound_session,
                    bound_owner,
                    Jsonb({"client": {"installation_id": installation_id}}),
                    unbound_session,
                    unbound_owner,
                    Jsonb(
                        {
                            "client": {
                                "installation_id": (
                                    "11111111-1111-1111-8111-111111111111"
                                )
                            }
                        }
                    ),
                ),
            )
            connection.execute(
                """
                insert into public.codex_sessions (id, user_id)
                values
                  (%s, %s), (%s, %s), (%s, %s), (%s, %s), (%s, %s),
                  (%s, %s)
                """,
                (
                    historical_repair,
                    repair_owner,
                    transcript_repair,
                    repair_owner,
                    transcript_other_agent,
                    repair_owner,
                    transcript_oversized_cache,
                    repair_owner,
                    unknown_source,
                    repair_owner,
                    historical_near_miss,
                    repair_owner,
                ),
            )
            connection.execute(
                """
                insert into public.codex_session_usage (
                  session_id, user_id, input_tokens, cached_input_tokens,
                  output_tokens, reasoning_output_tokens, total_tokens,
                  observed_at, metadata
                ) values
                  (%s, %s, 90, 60, 10, 2, 100, %s, %s),
                  (%s, %s, 100, 30, 20, 0, 160, %s, %s),
                  (%s, %s, 100, 30, 20, 0, 160, %s, %s),
                  (%s, %s, 100, 30, 20, 0, 160, %s, %s),
                  (%s, %s, 90, 60, 10, 2, 100, %s, %s),
                  (%s, %s, 90, 60, 10, 2, 101, %s, %s)
                """,
                (
                    historical_repair,
                    repair_owner,
                    "2026-07-27T09:00:00Z",
                    Jsonb({"source": "historical_transcript", "agent": "codex"}),
                    transcript_repair,
                    repair_owner,
                    "2026-07-27T09:00:00Z",
                    Jsonb(
                        {
                            "source": "transcript_sync",
                            "agent": "claude",
                            "cache_creation_input_tokens": 10,
                        }
                    ),
                    transcript_other_agent,
                    repair_owner,
                    "2026-07-27T09:00:00Z",
                    Jsonb(
                        {
                            "source": "transcript_sync",
                            "agent": "codex",
                            "cache_creation_input_tokens": 10,
                        }
                    ),
                    transcript_oversized_cache,
                    repair_owner,
                    "2026-07-27T09:00:00Z",
                    Jsonb(
                        {
                            "source": "transcript_sync",
                            "agent": "claude",
                            "cache_creation_input_tokens": "9" * 20,
                        }
                    ),
                    unknown_source,
                    repair_owner,
                    "2026-07-27T09:00:00Z",
                    Jsonb({"source": "unknown"}),
                    historical_near_miss,
                    repair_owner,
                    "2026-07-27T09:00:00Z",
                    Jsonb({"source": "historical_transcript", "agent": "codex"}),
                ),
            )
            connection.execute(USAGE_MIGRATION.read_text())
            connection.execute(REPAIR_MIGRATION.read_text())
            connection.execute(REPAIR_MIGRATION.read_text())
            repaired_rows = {
                row[0]: row[1:]
                for row in connection.execute(
                    """
                    select
                      session_id, user_id, input_tokens, cached_input_tokens,
                      output_tokens, reasoning_output_tokens, total_tokens,
                      observed_at
                    from public.codex_session_usage
                    where session_id in (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        historical_repair,
                        transcript_repair,
                        transcript_other_agent,
                        transcript_oversized_cache,
                        unknown_source,
                        historical_near_miss,
                    ),
                ).fetchall()
            }
            observed = repaired_rows[historical_repair][-1]
            assert repaired_rows == {
                historical_repair: (repair_owner, 30, 60, 8, 2, 100, observed),
                transcript_repair: (repair_owner, 100, 30, 20, 0, 150, observed),
                transcript_other_agent: (
                    repair_owner,
                    100,
                    30,
                    20,
                    0,
                    160,
                    observed,
                ),
                transcript_oversized_cache: (
                    repair_owner,
                    100,
                    30,
                    20,
                    0,
                    160,
                    observed,
                ),
                unknown_source: (repair_owner, 90, 60, 10, 2, 100, observed),
                historical_near_miss: (
                    repair_owner,
                    90,
                    60,
                    10,
                    2,
                    101,
                    observed,
                ),
            }, repaired_rows
            connection.execute(CAPABILITY_MIGRATION.read_text())
            connection.execute(
                """
                grant execute
                on function public.preserve_codex_session_binding()
                to public, anon, authenticated, service_role
                """
            )
            connection.execute(CAPABILITY_ACL_MIGRATION.read_text())

            trigger_signature = "public.preserve_codex_session_binding()"
            trigger_privileges = connection.execute(
                """
                select
                  has_function_privilege('anon', %s, 'execute'),
                  has_function_privilege('authenticated', %s, 'execute'),
                  has_function_privilege('service_role', %s, 'execute')
                """,
                (trigger_signature, trigger_signature, trigger_signature),
            ).fetchone()
            assert trigger_privileges == (False, False, False), trigger_privileges

            bindings = dict(
                connection.execute(
                    """
                    select id, installation_capability_sha256
                    from public.codex_sessions
                    where id in (%s, %s)
                    """,
                    (bound_session, unbound_session),
                ).fetchall()
            )
            assert bindings == {
                bound_session: expected_capability,
                unbound_session: None,
            }, bindings

            inserted_metadata = Jsonb(
                {"client": {"installation_id": inserted_installation_id}}
            )
            connection.execute(
                """
                insert into public.codex_sessions (id, user_id, metadata)
                values (%s, %s, %s)
                """,
                (inserted_without_digest, inserted_owner, inserted_metadata),
            )
            connection.execute(
                """
                insert into public.codex_sessions (
                  id, user_id, metadata, installation_capability_sha256
                ) values (%s, %s, %s, %s)
                """,
                (
                    inserted_with_wrong_digest,
                    inserted_owner,
                    inserted_metadata,
                    "f" * 64,
                ),
            )
            inserted_bindings = dict(
                connection.execute(
                    """
                    select id, installation_capability_sha256
                    from public.codex_sessions
                    where id in (%s, %s)
                    """,
                    (inserted_without_digest, inserted_with_wrong_digest),
                ).fetchall()
            )
            assert inserted_bindings == {
                inserted_without_digest: inserted_capability,
                inserted_with_wrong_digest: inserted_capability,
            }, inserted_bindings

            replacement_owner = uuid.uuid4()
            replacement_capability = "f" * 64
            connection.execute(
                """
                update public.codex_sessions
                set user_id = %s, installation_capability_sha256 = %s
                where id in (%s, %s)
                """,
                (
                    replacement_owner,
                    replacement_capability,
                    bound_session,
                    unbound_session,
                ),
            )
            connection.execute(
                """
                update public.codex_sessions
                set
                  user_id = %s,
                  installation_capability_sha256 = case
                    when id = %s then null
                    else %s
                  end
                where id in (%s, %s)
                """,
                (
                    replacement_owner,
                    inserted_without_digest,
                    replacement_capability,
                    inserted_without_digest,
                    inserted_with_wrong_digest,
                ),
            )
            preserved = {
                row[0]: (row[1], row[2])
                for row in connection.execute(
                    """
                    select id, user_id, installation_capability_sha256
                    from public.codex_sessions
                    where id in (%s, %s, %s, %s)
                    """,
                    (
                        bound_session,
                        unbound_session,
                        inserted_without_digest,
                        inserted_with_wrong_digest,
                    ),
                ).fetchall()
            }
            assert preserved == {
                bound_session: (bound_owner, expected_capability),
                unbound_session: (unbound_owner, None),
                inserted_without_digest: (inserted_owner, inserted_capability),
                inserted_with_wrong_digest: (inserted_owner, inserted_capability),
            }, preserved

            signature = (
                "public.upsert_codex_session_usage_latest(text,bigint,bigint,"
                "bigint,bigint,bigint,bigint,timestamp with time zone,jsonb)"
            )
            privileges = connection.execute(
                """
                select
                  has_function_privilege('anon', %s, 'execute'),
                  has_function_privilege('authenticated', %s, 'execute'),
                  has_function_privilege('service_role', %s, 'execute')
                """,
                (signature, signature, signature),
            ).fetchone()
            assert privileges == (False, False, True), privileges

            owner = uuid.uuid4()
            wrong_owner = uuid.uuid4()
            concurrent_session = str(uuid.uuid4())
            conflict_session = str(uuid.uuid4())
            connection.execute(
                """
                insert into public.codex_sessions (id, user_id)
                values (%s, %s), (%s, %s)
                """,
                (concurrent_session, owner, conflict_session, owner),
            )
            connection.execute(
                """
                insert into public.codex_session_usage (
                  session_id, user_id, input_tokens, cached_input_tokens,
                  output_tokens, reasoning_output_tokens, total_tokens, observed_at
                ) values (%s, %s, 8, 0, 2, 0, 10, now())
                """,
                (conflict_session, wrong_owner),
            )

        try:
            invoke(test_url, conflict_session, 20, "2026-07-27T10:00:00Z")
        except psycopg.errors.CheckViolation:
            pass
        else:
            raise AssertionError("owner conflict was not rejected")

        barrier = threading.Barrier(2)

        def concurrent(total: int, observed_at: str) -> None:
            barrier.wait()
            invoke(test_url, concurrent_session, total, observed_at)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(concurrent, 100, "2026-07-27T10:00:00Z"),
                executor.submit(concurrent, 200, "2026-07-27T10:05:00Z"),
            ]
            for future in futures:
                future.result()

        with psycopg.connect(test_url) as connection:
            row = connection.execute(
                """
                select user_id, total_tokens, observed_at
                from public.codex_session_usage where session_id = %s
                """,
                (concurrent_session,),
            ).fetchone()
            assert row is not None
            assert row[0] == owner
            assert row[1] == 200
            assert row[2].isoformat() == "2026-07-27T10:05:00+00:00"
        print("usage RPC database integration: PASS")
    finally:
        with psycopg.connect(**admin_parameters, autocommit=True) as admin:
            admin.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity where datname = %s",
                (database_name,),
            )
            admin.execute(sql.SQL("drop database {}").format(sql.Identifier(database_name)))


if __name__ == "__main__":
    main()
