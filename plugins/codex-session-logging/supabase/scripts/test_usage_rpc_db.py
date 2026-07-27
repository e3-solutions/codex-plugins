#!/usr/bin/env python3
"""Apply and exercise the usage RPC migration against a disposable local database."""

from __future__ import annotations

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
MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "20260727053107_upsert_codex_session_usage_latest.sql"
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
        with psycopg.connect(test_url, autocommit=True) as connection:
            connection.execute(
                """
                create table public.codex_sessions (
                  id text primary key,
                  user_id uuid not null
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
            connection.execute(MIGRATION.read_text())

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
                "insert into public.codex_sessions values (%s, %s), (%s, %s)",
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
