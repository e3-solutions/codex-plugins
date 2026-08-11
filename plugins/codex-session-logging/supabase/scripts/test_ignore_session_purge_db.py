#!/usr/bin/env python3
"""Exercise indexed ignored-session Storage purging in disposable Postgres."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict


DATABASE_URL_ENV = "CODEX_SESSION_LOG_TEST_DATABASE_URL"
MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "20260811222845_index_ignored_session_object_lookup.sql"
)


def _plan_index_nodes(node: dict[str, object]) -> list[dict[str, object]]:
    matches = []
    if node.get("Index Name") == "idx_objects_bucket_id_name":
        matches.append(node)
    for child in node.get("Plans", []):
        if isinstance(child, dict):
            matches.extend(_plan_index_nodes(child))
    return matches


def main() -> None:
    base_url = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not base_url:
        raise SystemExit(f"{DATABASE_URL_ENV} is required")

    parameters = conninfo_to_dict(base_url)
    admin_parameters = {**parameters, "dbname": "postgres"}
    database_name = f"codex_ignore_purge_test_{uuid.uuid4().hex}"
    test_parameters = {**parameters, "dbname": database_name}

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
        session_id = str(uuid.uuid4())
        session_hash = hashlib.sha256(
            f"codex_rollout_session:{session_id}".encode()
        ).hexdigest()
        bucket = "codex-sessions"
        prefix = f"users/test-user/sessions/{session_id}"
        expected_paths = {f"{prefix}/messages/a.json", f"{prefix}/rollout/b.json"}

        with psycopg.connect(**test_parameters, autocommit=True) as connection:
            connection.execute(
                """
                create schema extensions;
                create extension pgcrypto with schema extensions;
                create schema storage;
                create table storage.objects (
                  id bigint generated always as identity primary key,
                  bucket_id text not null,
                  name text not null
                );
                create index idx_objects_bucket_id_name
                  on storage.objects (bucket_id, name collate "C");
                create table public.codex_ignored_sessions (
                  session_id_hash text primary key,
                  ignored_at timestamptz not null
                );
                create table public.codex_session_storage_locators (
                  session_id_hash text not null,
                  user_id text not null,
                  storage_bucket text not null,
                  storage_prefix text not null,
                  primary key (session_id_hash, storage_bucket, storage_prefix)
                );
                create table public.codex_sessions (id text primary key);
                create function public.codex_session_is_ignored(p_session_id text)
                returns boolean
                language sql
                stable
                security definer
                set search_path = ''
                as $function$
                  select exists (
                    select 1
                    from public.codex_ignored_sessions ignored
                    where ignored.session_id_hash = encode(
                      extensions.digest(
                        'codex_rollout_session:' || p_session_id,
                        'sha256'
                      ),
                      'hex'
                    )
                  )
                $function$;
                """
            )
            connection.execute(MIGRATION.read_text(encoding="utf-8"))
            connection.execute(
                "insert into public.codex_sessions(id) values (%s)",
                (session_id,),
            )
            connection.execute(
                """insert into public.codex_ignored_sessions(
                     session_id_hash, ignored_at
                   ) values (%s, clock_timestamp() - interval '20 minutes')""",
                (session_hash,),
            )
            connection.execute(
                """insert into public.codex_session_storage_locators(
                     session_id_hash, user_id, storage_bucket, storage_prefix
                   ) values (%s, 'test-user', %s, %s)""",
                (session_hash, bucket, prefix),
            )
            connection.execute(
                """
                insert into storage.objects(bucket_id, name)
                select %s,
                  case
                    when value <= 50000 then 'aaa-decoys/' || value
                    else 'zzz-decoys/' || value
                  end
                from generate_series(1, 100000) value
                """,
                (bucket,),
            )
            with connection.cursor() as cursor:
                cursor.executemany(
                    "insert into storage.objects(bucket_id, name) values (%s, %s)",
                    [
                        (bucket, path) for path in sorted(expected_paths)
                    ]
                    + [
                        (bucket, prefix),
                        (bucket, f"{prefix}0"),
                        (bucket, f"{prefix}-neighbor"),
                        ("other-bucket", f"{prefix}/messages/other.json"),
                    ],
                )
            connection.execute("analyze storage.objects")
            connection.execute(
                "analyze public.codex_session_storage_locators"
            )

            page = connection.execute(
                "select public.list_ignored_codex_session_objects(%s, 1000)",
                (session_id,),
            ).fetchone()[0]
            actual_paths = {item["path"] for item in page["items"]}
            assert page["status"] == "pending"
            assert actual_paths == expected_paths
            assert {item["bucket"] for item in page["items"]} == {bucket}

            plan = connection.execute(
                """
                explain (analyze, buffers, format json)
                select object_for_locator.bucket_id, object_for_locator.name
                from public.codex_session_storage_locators locator
                cross join lateral (
                  select stored.bucket_id, stored.name
                  from storage.objects stored
                  where stored.bucket_id = locator.storage_bucket
                    and stored.name collate "C" >=
                      (locator.storage_prefix || '/') collate "C"
                    and stored.name collate "C" <
                      (locator.storage_prefix || '0') collate "C"
                  limit 1000
                ) object_for_locator
                where locator.session_id_hash = %s
                limit 1000
                """,
                (session_hash,),
            ).fetchone()[0][0]["Plan"]
            matching_index_nodes = _plan_index_nodes(plan)
            assert len(matching_index_nodes) == 1, json.dumps(plan, sort_keys=True)
            index_condition = str(matching_index_nodes[0].get("Index Cond", ""))
            assert "bucket_id =" in index_condition
            assert "name >=" in index_condition
            assert "name <" in index_condition

            pending = connection.execute(
                "select public.finalize_ignored_codex_session_purge(%s)",
                (session_id,),
            ).fetchone()[0]
            assert pending["status"] == "pending_storage"
            connection.execute(
                "delete from storage.objects where bucket_id = %s and name = any(%s)",
                (bucket, list(expected_paths)),
            )
            final = connection.execute(
                "select public.finalize_ignored_codex_session_purge(%s)",
                (session_id,),
            ).fetchone()[0]
            assert final["status"] == "deleted"
            counts = connection.execute(
                """
                select
                  (select count(*) from public.codex_sessions),
                  (select count(*) from public.codex_session_storage_locators),
                  (select count(*) from public.codex_ignored_sessions),
                  (select count(*) from storage.objects)
                """
            ).fetchone()
            assert counts == (0, 0, 1, 100004)
            print(json.dumps({"status": "ok", "decoys_verified": counts[3]}))
    finally:
        with psycopg.connect(**admin_parameters, autocommit=True) as admin:
            admin.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity where datname = %s",
                (database_name,),
            )
            admin.execute(
                sql.SQL("drop database if exists {}").format(
                    sql.Identifier(database_name)
                )
            )


if __name__ == "__main__":
    main()
