from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import threading
import urllib.error
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "codex-session-logging"
SCRIPTS = PLUGIN / "scripts"
REPROCESS = PLUGIN / "supabase" / "scripts" / "reprocess_rollout_usage.py"


def load_module(name: str, path: Path):
    sys.path.insert(0, str(SCRIPTS))
    try:
        sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS))


def usage_envelope(timestamp: str, total: int, *, context: int | None = 258400) -> dict:
    info = {
        "total_token_usage": {
            "input_tokens": total - 10,
            "cached_input_tokens": total // 2,
            "output_tokens": 10,
            "reasoning_output_tokens": 2,
            "total_tokens": total,
        }
    }
    if context is not None:
        info["model_context_window"] = context
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {"type": "token_count", "info": info},
    }


def test_shared_parser_normalizes_inclusive_components_and_is_monotonic():
    usage = load_module("rollout_usage", SCRIPTS / "rollout_usage.py")
    parsed = usage.parse_cumulative_usage(usage_envelope("2026-07-26T10:00:00Z", 120))

    assert parsed == {
        "input_tokens": 50,
        "cached_input_tokens": 60,
        "output_tokens": 8,
        "reasoning_output_tokens": 2,
        "total_tokens": 120,
        "created_at": "2026-07-26T10:00:00Z",
        "model_context_window": 258400,
    }
    assert usage.latest_cumulative_usage(
        [
            usage_envelope("2026-07-26T10:00:00Z", 900),
            usage_envelope("2026-07-26T10:05:00Z", 100),
        ]
    )["total_tokens"] == 900
    invalid = usage_envelope("2026-07-26T10:05:00Z", 100)
    invalid["payload"]["info"]["total_token_usage"]["total_tokens"] = 101
    assert usage.parse_cumulative_usage(invalid) is None
    missing_timestamp = usage_envelope("2026-07-26T10:05:00Z", 100)
    missing_timestamp.pop("timestamp")
    assert usage.parse_cumulative_usage(missing_timestamp) is None
    assert usage.parse_cumulative_usage(
        usage_envelope("2026-07-26T10:05:00", 100)
    ) is None
    equivalent = dict(parsed)
    equivalent["created_at"] = "2026-07-26T03:00:00-07:00"
    assert usage.usage_observation_id("session", parsed) == usage.usage_observation_id(
        "session", equivalent
    )


class FakeClient:
    def __init__(self, events: list[dict], objects: dict[tuple[str, str], bytes]):
        self.events = events
        self.objects = objects
        self.upserts: list[dict] = []
        self.queries: list[dict] = []
        self.downloads: list[tuple[str, str]] = []

    def iter_rollout_events(self, **query):
        self.queries.append(query)
        rows = self.events
        if query["after_session"]:
            rows = [row for row in rows if row["session_id"] > query["after_session"]]
        yield from sorted(rows, key=lambda row: (row["session_id"], row["id"]))

    def download(self, bucket: str, storage_path: str) -> bytes:
        self.downloads.append((bucket, storage_path))
        return self.objects[(bucket, storage_path)]

    def upsert_usage(self, parameters: dict) -> None:
        self.upserts.append(parameters)


class ParallelFakeClient(FakeClient):
    def __init__(self, events: list[dict], objects: dict[tuple[str, str], bytes]):
        super().__init__(events, objects)
        self.download_barrier = threading.Barrier(2)
        self.download_threads: set[int] = set()

    def download(self, bucket: str, storage_path: str) -> bytes:
        self.download_threads.add(threading.get_ident())
        self.download_barrier.wait(timeout=2)
        return super().download(bucket, storage_path)


def stored_generation(
    session_id: str,
    generation: str,
    raw: bytes,
    *,
    cuts: tuple[int, ...] = (),
) -> tuple[list[dict], dict[tuple[str, str], bytes]]:
    boundaries = (0, *cuts, len(raw))
    events = []
    objects = {}
    for start, end in zip(boundaries, boundaries[1:]):
        content = raw[start:end]
        digest = hashlib.sha256(content).hexdigest()
        path = f"sessions/{session_id}/{generation}/{start}-{end}.jsonl"
        events.append(
            {
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, path)),
                "session_id": session_id,
                "storage_bucket": "codex-sessions",
                "storage_path": path,
                "metadata": {
                    "file_generation": generation,
                    "start_offset": start,
                    "end_offset": end,
                    "content_byte_size": len(content),
                    "content_sha256": digest,
                },
            }
        )
        objects[("codex-sessions", path)] = content
    return events, objects


def rollout_bytes(*envelopes: dict) -> bytes:
    return b"".join(json.dumps(value).encode() + b"\n" for value in envelopes)


def test_replay_is_bounded_dry_run_then_applies_once_without_owner_input():
    replay = load_module("reprocess_rollout_usage", REPROCESS)
    first = "11111111-1111-4111-8111-111111111111"
    second = "22222222-2222-4222-8222-222222222222"
    first_events, first_objects = stored_generation(
        first,
        "a" * 16,
        rollout_bytes(usage_envelope("2026-07-26T10:05:00Z", 220, context=None)),
        cuts=(17,),
    )
    second_events, second_objects = stored_generation(
        second,
        "b" * 16,
        rollout_bytes(usage_envelope("2026-07-26T10:06:00Z", 300)),
    )
    client = FakeClient(
        [*second_events, *first_events],
        {**first_objects, **second_objects},
    )

    dry_run = replay.reprocess_rollout_usage(
        client,
        cutoff="2026-07-27T00:00:00Z",
        lookback_hours=48,
        max_sessions=1,
    )
    assert dry_run["mode"] == "dry-run"
    assert dry_run["since"] == "2026-07-25T00:00:00Z"
    assert dry_run["cutoff"] == "2026-07-27T00:00:00Z"
    assert dry_run["sessions"] == 1
    assert dry_run["truncated"] is True
    assert dry_run["resume_after_session"] == first
    assert client.upserts == []

    applied = replay.reprocess_rollout_usage(
        client,
        apply=True,
        cutoff=dry_run["cutoff"],
        lookback_hours=48,
        max_sessions=1,
    )
    assert applied["rpc_calls"] == 1
    assert client.upserts[0]["p_session_id"] == first
    assert client.upserts[0]["p_total_tokens"] == 220
    assert "p_user_id" not in client.upserts[0]


def test_replay_reads_independent_sessions_with_bounded_workers():
    replay = load_module("reprocess_rollout_usage_workers", REPROCESS)
    first = "11111111-1111-4111-8111-111111111111"
    second = "22222222-2222-4222-8222-222222222222"
    first_events, first_objects = stored_generation(
        first,
        "a" * 16,
        rollout_bytes(usage_envelope("2026-07-26T10:05:00Z", 220)),
    )
    second_events, second_objects = stored_generation(
        second,
        "b" * 16,
        rollout_bytes(usage_envelope("2026-07-26T10:06:00Z", 300)),
    )
    client = ParallelFakeClient(
        [*first_events, *second_events],
        {**first_objects, **second_objects},
    )

    result = replay.reprocess_rollout_usage(
        client,
        cutoff="2026-07-27T00:00:00Z",
        workers=2,
    )

    assert result["sessions"] == 2
    assert result["observations"] == 2
    assert len(client.download_threads) == 2


@pytest.mark.parametrize("workers", [0, 5])
def test_replay_rejects_unbounded_worker_counts(workers):
    replay = load_module(f"reprocess_rollout_usage_workers_{workers}", REPROCESS)

    with pytest.raises(replay.ReplayError, match="workers must be between 1 and 4"):
        replay.reprocess_rollout_usage(
            FakeClient([], {}),
            cutoff="2026-07-27T00:00:00Z",
            workers=workers,
        )


def test_replay_rejects_non_contiguous_chunks_without_writing():
    replay = load_module("reprocess_rollout_usage_gap", REPROCESS)
    session_id = "11111111-1111-4111-8111-111111111111"
    events, objects = stored_generation(
        session_id,
        "c" * 16,
        rollout_bytes(usage_envelope("2026-07-26T10:00:00Z", 100)),
        cuts=(20,),
    )
    events[1]["metadata"]["start_offset"] = 21
    events[1]["metadata"]["content_byte_size"] -= 1
    client = FakeClient(events, objects)

    result = replay.reprocess_rollout_usage(
        client,
        apply=True,
        cutoff="2026-07-27T00:00:00Z",
    )

    assert result["rpc_calls"] == 0
    assert "non-contiguous offset" in result["errors"][0]
    assert client.upserts == []


def test_replay_recovers_valid_final_token_count_without_newline():
    replay = load_module("reprocess_rollout_usage_no_newline", REPROCESS)
    session_id = "11111111-1111-4111-8111-111111111111"
    raw = json.dumps(
        usage_envelope("2026-07-26T10:00:00Z", 100),
        separators=(",", ":"),
    ).encode()
    events, objects = stored_generation(session_id, "d" * 16, raw, cuts=(19,))
    client = FakeClient(events, objects)

    result = replay.reprocess_rollout_usage(
        client,
        apply=True,
        cutoff="2026-07-27T00:00:00Z",
    )

    assert result["sessions_with_usage"] == 1
    assert result["rpc_calls"] == 1
    assert client.upserts[0]["p_session_id"] == session_id
    assert client.upserts[0]["p_total_tokens"] == 100


def test_replay_applies_every_distinct_observation_including_regression():
    replay = load_module("reprocess_rollout_usage_all_observations", REPROCESS)
    session_id = "11111111-1111-4111-8111-111111111111"
    first = usage_envelope("2026-07-26T10:00:00Z", 100)
    second = usage_envelope("2026-07-26T10:05:00Z", 200)
    regression = usage_envelope("2026-07-26T10:10:00Z", 150)
    events, objects = stored_generation(
        session_id,
        "e" * 16,
        rollout_bytes(first, second, second, regression),
    )
    client = FakeClient(events, objects)

    result = replay.reprocess_rollout_usage(
        client,
        apply=True,
        cutoff="2026-07-27T00:00:00Z",
    )

    assert result["observations"] == 3
    assert result["rpc_calls"] == 3
    assert [parameters["p_total_tokens"] for parameters in client.upserts] == [
        100,
        200,
        150,
    ]
    assert all(
        parameters["p_cached_input_tokens"] > 0
        for parameters in client.upserts
    )


def test_replay_quarantines_exact_legacy_session_and_continues():
    replay = load_module("reprocess_rollout_usage_legacy", REPROCESS)
    legacy_session = "11111111-1111-4111-8111-111111111111"
    valid_session = "22222222-2222-4222-8222-222222222222"
    tail_events, tail_objects = stored_generation(
        legacy_session,
        "a" * 16,
        rollout_bytes(usage_envelope("2026-07-26T10:05:00Z", 220)),
    )
    tail = tail_events[0]
    size = tail["metadata"]["content_byte_size"]
    tail["metadata"]["start_offset"] = 1024
    tail["metadata"]["end_offset"] = 1024 + size
    legacy = {
        **tail,
        "id": "00000000-0000-4000-8000-000000000001",
        "storage_path": "legacy/event.json",
        "metadata": {"source": "pre-specialized-ingest"},
    }
    valid_events, valid_objects = stored_generation(
        valid_session,
        "b" * 16,
        rollout_bytes(usage_envelope("2026-07-26T10:06:00Z", 300)),
    )

    apply_client = FakeClient(
        [legacy, tail, *valid_events],
        {**tail_objects, **valid_objects},
    )
    applied = replay.reprocess_rollout_usage(
        apply_client,
        apply=True,
        cutoff="2026-07-27T00:00:00Z",
    )
    assert applied["legacy_events_quarantined"] == 1
    assert applied["legacy_sessions_quarantined"] == 1
    assert len(applied["errors"]) == 1
    assert "legacy rollout events" in applied["errors"][0]
    assert applied["sessions_with_usage"] == 1
    assert applied["rpc_calls"] == 1
    assert applied["resume_after_session"] == valid_session
    assert apply_client.downloads == [
        ("codex-sessions", valid_events[0]["storage_path"])
    ]
    assert [row["p_session_id"] for row in apply_client.upserts] == [valid_session]


def test_replay_does_not_classify_partial_chunk_metadata_as_legacy():
    replay = load_module("reprocess_rollout_usage_partial", REPROCESS)
    session_id = "11111111-1111-4111-8111-111111111111"
    client = FakeClient(
        [
            {
                "id": "00000000-0000-4000-8000-000000000001",
                "session_id": session_id,
                "storage_bucket": "codex-sessions",
                "storage_path": "partial/chunk.jsonl",
                "metadata": {"start_offset": 0},
            }
        ],
        {},
    )

    with pytest.raises(replay.ReplayError, match="file_generation"):
        replay.reprocess_rollout_usage(
            client,
            apply=True,
            cutoff="2026-07-27T00:00:00Z",
        )
    assert client.downloads == []
    assert client.upserts == []


class FakeHttpResponse:
    def __init__(
        self, content: bytes = b"ok", read_error: Exception | None = None
    ) -> None:
        self.content = content
        self.read_error = read_error

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        if self.read_error is not None:
            raise self.read_error
        return self.content


@pytest.mark.parametrize(
    "failure",
    ["read-reset", "http-503"],
)
def test_storage_download_retries_transient_failures(monkeypatch, failure):
    replay = load_module("reprocess_rollout_usage_retry", REPROCESS)
    calls = 0
    delays: list[float] = []

    def urlopen(_request, timeout):
        nonlocal calls
        assert timeout == 60
        calls += 1
        if calls == 1:
            if failure == "read-reset":
                return FakeHttpResponse(read_error=ConnectionResetError("reset"))
            raise urllib.error.HTTPError(
                "https://example", 503, "unavailable", None, None
            )
        return FakeHttpResponse()

    monkeypatch.setattr(replay.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(replay.time, "sleep", delays.append)
    client = replay.SupabaseAdminClient("https://example.supabase.co", "secret")

    assert client.download("bucket", "path") == b"ok"
    assert calls == 2
    assert delays == [0.25]


def test_storage_download_stops_after_three_transient_failures(monkeypatch):
    replay = load_module("reprocess_rollout_usage_retry_limit", REPROCESS)
    calls = 0
    delays: list[float] = []

    def urlopen(_request, timeout):
        nonlocal calls
        assert timeout == 60
        calls += 1
        raise ConnectionResetError("reset")

    monkeypatch.setattr(replay.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(replay.time, "sleep", delays.append)
    client = replay.SupabaseAdminClient("https://example.supabase.co", "secret")

    with pytest.raises(replay.TransientRequestError):
        client.download("bucket", "path")
    assert calls == 3
    assert delays == [0.25, 0.5]


def test_rpc_transient_failure_is_not_retried(monkeypatch):
    replay = load_module("reprocess_rollout_usage_no_rpc_retry", REPROCESS)
    calls = 0

    def urlopen(_request, timeout):
        nonlocal calls
        assert timeout == 60
        calls += 1
        raise urllib.error.HTTPError(
            "https://example", 503, "request failed", None, None
        )

    monkeypatch.setattr(replay.urllib.request, "urlopen", urlopen)
    client = replay.SupabaseAdminClient("https://example.supabase.co", "secret")

    with pytest.raises(replay.ReplayError):
        client.upsert_usage({"p_session_id": "session"})
    assert calls == 1


def test_supabase_replay_uses_catalog_cutoff_and_keyset_without_snapshot_writes():
    replay = load_module("reprocess_rollout_usage_keyset", REPROCESS)
    client = replay.SupabaseAdminClient("https://example.supabase.co", "secret")
    paths: list[tuple[str, str]] = []
    rows = [
        {
            "id": "00000000-0000-4000-8000-000000000001",
            "session_id": "11111111-1111-4111-8111-111111111111",
        }
    ]

    def request_json(method: str, path: str, payload=None):
        paths.append((method, path))
        return rows if len(paths) == 1 else []

    client.request_json = request_json
    assert list(
        client.iter_rollout_events(
            page_size=1,
            since="2026-07-24T00:00:00Z",
            cutoff="2026-07-27T00:00:00Z",
            after_session=None,
        )
    ) == rows
    assert all(method == "GET" for method, _ in paths)
    assert "codex_session_events" in paths[0][1]
    assert "created_at=gte." in paths[0][1]
    assert "created_at=lte." in paths[0][1]
    assert "or=" in paths[1][1]
    assert "offset=" not in paths[1][1]
    assert "snapshot" not in "".join(path for _, path in paths)


def test_usage_rpc_migration_derives_owner_and_is_service_role_only():
    migration = next(
        (PLUGIN / "supabase" / "migrations").glob(
            "*upsert_codex_session_usage_latest.sql"
        )
    )
    sql = migration.read_text().lower()

    assert "set search_path = ''" in sql
    assert "security invoker" in sql
    assert "from public.codex_sessions" in sql
    assert "values (\n    p_session_id,\n    session_owner," in sql
    assert "user_id = excluded.user_id" not in sql
    assert "from public, anon, authenticated, service_role" in sql
    assert ") to service_role" in sql
    assert "excluded.total_tokens >= public.codex_session_usage.total_tokens" in sql
    assert "excluded.observed_at >= public.codex_session_usage.observed_at" in sql
    assert "codex_rollout_replay_snapshot" not in sql


def test_observation_migration_is_append_only_indexed_and_keeps_old_rpc():
    migration = next(
        (PLUGIN / "supabase" / "migrations").glob(
            "*codex_session_usage_observations.sql"
        )
    )
    sql = migration.read_text().lower()
    table_sql = sql.split(
        "create table public.codex_session_usage_observations (", 1
    )[1].split(");", 1)[0]

    assert "id uuid primary key" in table_sql
    assert "references public.codex_sessions(id) on delete cascade" in table_sql
    assert "cached_input_tokens bigint not null" in table_sql
    assert "reasoning_output_tokens bigint not null" in table_sql
    assert "observed_at timestamptz not null" in table_sql
    assert "total_tokens" not in table_sql
    assert "model_context_window" not in table_sql
    assert "created_at" not in table_sql
    assert "(start, end]" in sql
    assert "observed_at desc,\n    session_id" in sql
    assert "session_id,\n    observed_at desc,\n    id desc" in sql
    assert "enable row level security" in sql
    assert "from pg_catalog.pg_roles" in sql
    assert "create role codestat_ro nologin" in sql
    assert "to codestat_ro\n  using (true)" in sql
    assert "to codestat_ro;" in sql
    assert "grant select, insert" in sql
    assert "grant update" not in sql
    assert "grant delete" not in sql
    assert "create or replace function public.upsert_codex_session_usage_latest(" in sql
    assert "on conflict (id) do nothing" in sql
    assert "at time zone 'utc'" in sql
    assert "excluded.input_tokens >= public.codex_session_usage.input_tokens" in sql
    assert (
        "excluded.cached_input_tokens\n      >= "
        "public.codex_session_usage.cached_input_tokens"
    ) in sql
    assert "excluded.output_tokens >= public.codex_session_usage.output_tokens" in sql
    assert (
        "excluded.reasoning_output_tokens\n      >= "
        "public.codex_session_usage.reasoning_output_tokens"
    ) in sql
    assert "excluded.total_tokens >= public.codex_session_usage.total_tokens" in sql
    assert "excluded.observed_at >= public.codex_session_usage.observed_at" in sql


def test_additive_repair_migration_is_source_and_pattern_gated():
    migration = next(
        (PLUGIN / "supabase" / "migrations").glob(
            "*repair_additive_session_usage.sql"
        )
    )
    sql = migration.read_text().lower()

    assert "metadata ->> 'source' = 'historical_transcript'" in sql
    assert "input_tokens >= cached_input_tokens" in sql
    assert "output_tokens >= reasoning_output_tokens" in sql
    assert "metadata ->> 'source' = 'transcript_sync'" in sql
    assert "metadata ->> 'agent' = 'claude'" in sql
    assert "cache_creation_input_tokens" in sql
    assert "'^[0-9]{1,19}$'" in sql
    assert "<= 9223372036854775807::numeric" in sql
    assert "total_tokens::numeric" in sql
    assert "validate constraint" not in sql
