from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import uuid
from pathlib import Path


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
        },
    }
    if context is not None:
        info["model_context_window"] = context
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {"type": "token_count", "info": info},
    }


def test_shared_parser_selects_newest_timestamp_then_larger_equal_time_total():
    usage = load_module("rollout_usage", SCRIPTS / "rollout_usage.py")
    envelopes = [
        usage_envelope("2026-07-26T10:05:00Z", 100),
        usage_envelope("2026-07-26T10:00:00Z", 900),
        usage_envelope("2026-07-26T10:05:00+00:00", 120, context=None),
    ]

    latest = usage.latest_cumulative_usage(envelopes)

    assert latest == {
        "input_tokens": 50,
        "cached_input_tokens": 60,
        "output_tokens": 8,
        "reasoning_output_tokens": 2,
        "total_tokens": 120,
        "created_at": "2026-07-26T10:05:00+00:00",
    }
    assert usage.parse_cumulative_usage({"type": "event_msg", "payload": {}}) is None
    assert usage.parse_cumulative_usage(
        usage_envelope("2026-07-26T10:05:00Z", True)  # bools are not token counts
    ) is None


def test_shared_parser_never_regresses_a_cumulative_total() -> None:
    usage = load_module("rollout_usage_monotonic", SCRIPTS / "rollout_usage.py")
    current = usage.parse_cumulative_usage(
        usage_envelope("2026-07-26T10:00:00Z", 900)
    )
    later_but_smaller = usage.parse_cumulative_usage(
        usage_envelope("2026-07-26T10:05:00Z", 100)
    )

    assert current is not None
    assert later_but_smaller is not None
    assert usage.usage_is_newer(later_but_smaller, current) is False
    assert usage.latest_cumulative_usage([
        usage_envelope("2026-07-26T10:00:00Z", 900),
        usage_envelope("2026-07-26T10:05:00Z", 100),
    ])["total_tokens"] == 900


def test_shared_parser_normalizes_inclusive_codex_components_and_rejects_inconsistency():
    usage = load_module("rollout_usage_normalization", SCRIPTS / "rollout_usage.py")
    normalized = usage.parse_cumulative_usage({
        "timestamp": "2026-07-26T12:01:00Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {
                    "input_tokens": 180,
                    "cached_input_tokens": 100,
                    "output_tokens": 35,
                    "reasoning_output_tokens": 5,
                    "total_tokens": 215,
                },
            },
        },
    })

    assert normalized is not None
    assert {
        key: normalized[key]
        for key in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "total_tokens",
        )
    } == {
        "input_tokens": 80,
        "cached_input_tokens": 100,
        "output_tokens": 30,
        "reasoning_output_tokens": 5,
        "total_tokens": 215,
    }

    invalid = usage_envelope("2026-07-26T12:01:00Z", 215)
    invalid["payload"]["info"]["total_token_usage"]["total_tokens"] = 216
    assert usage.parse_cumulative_usage(invalid) is None

    impossible_cache = usage_envelope("2026-07-26T12:01:00Z", 20)
    impossible_cache["payload"]["info"]["total_token_usage"]["cached_input_tokens"] = 11
    assert usage.parse_cumulative_usage(impossible_cache) is None


class FakeClient:
    def __init__(self, events: list[dict], objects: dict[tuple[str, str], bytes]):
        self.events = events
        self.objects = objects
        self.upserts: list[dict] = []
        self.snapshots: dict[str, list[dict]] = {}
        self.next_snapshot_number = 1

    def insert_events(self, events: list[dict]) -> None:
        self.events.extend(events)

    def create_rollout_replay_snapshot(self) -> tuple[str, int]:
        snapshot_id = str(uuid.UUID(int=self.next_snapshot_number, version=4))
        self.next_snapshot_number += 1
        self.snapshots[snapshot_id] = json.loads(json.dumps(self.events))
        return snapshot_id, len(self.events)

    def rollout_replay_snapshot_event_count(self, snapshot_id: str) -> int:
        return len(self.snapshots[snapshot_id])

    def iter_rollout_events(
        self,
        *,
        page_size: int,
        after_session: str | None,
        snapshot_id: str,
    ):
        assert page_size > 0
        eligible = (
            row for row in self.snapshots[snapshot_id]
            if (after_session is None or row["session_id"] > after_session)
        )
        yield from sorted(
            eligible,
            key=lambda row: (row["session_id"], row["id"]),
        )

    def download(self, bucket: str, storage_path: str) -> bytes:
        return self.objects[(bucket, storage_path)]

    def upsert_usage(self, parameters: dict) -> None:
        self.upserts.append(parameters)


def stored_generation(
    session_id: str,
    user_id: str,
    generation: str,
    raw: bytes,
    *,
    cuts: tuple[int, ...],
) -> tuple[list[dict], dict[tuple[str, str], bytes]]:
    boundaries = (0, *cuts, len(raw))
    events = []
    objects = {}
    for start, end in zip(boundaries, boundaries[1:]):
        content = raw[start:end]
        digest = hashlib.sha256(content).hexdigest()
        storage_path = f"users/u/sessions/{session_id}/{generation}/{start}-{end}.jsonl"
        events.append({
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, storage_path)),
            "created_at": "2026-07-26T10:00:00Z",
            "session_id": session_id,
            "user_id": user_id,
            "storage_bucket": "codex-sessions",
            "storage_path": storage_path,
            "metadata": {
                "file_generation": generation,
                "start_offset": start,
                "end_offset": end,
                "content_byte_size": len(content),
                "content_sha256": digest,
            },
        })
        objects[("codex-sessions", storage_path)] = content
    return events, objects


def rollout_bytes(*envelopes: dict) -> bytes:
    return b"".join(
        json.dumps(envelope, separators=(",", ":")).encode() + b"\n"
        for envelope in envelopes
    )


def test_reprocess_verifies_streams_and_calls_rpc_once_per_session():
    reprocess = load_module("reprocess_rollout_usage", REPROCESS)
    session_id = "11111111-1111-4111-8111-111111111111"
    user_id = "22222222-2222-4222-8222-222222222222"
    older = rollout_bytes(usage_envelope("2026-07-26T10:00:00Z", 100))
    latest = rollout_bytes(
        usage_envelope("2026-07-26T10:05:00Z", 200),
        usage_envelope("2026-07-26T10:05:00Z", 220, context=None),
    )
    older_events, older_objects = stored_generation(
        session_id,
        user_id,
        "a" * 16,
        older,
        cuts=(17,),
    )
    latest_events, latest_objects = stored_generation(
        session_id,
        user_id,
        "b" * 16,
        latest,
        cuts=(11, len(latest) - 5),
    )
    client = FakeClient(
        [*latest_events, *older_events],
        {**older_objects, **latest_objects},
    )
    dry_run = reprocess.reprocess_rollout_usage(client)
    applied = reprocess.reprocess_rollout_usage(client, apply=True)

    assert dry_run == {
        "mode": "dry-run",
        "events": 5,
        "generations": 2,
        "sessions": 1,
        "sessions_with_usage": 1,
        "rpc_calls": 0,
        "errors": [],
        "resume_after_session": session_id,
        "snapshot_id": str(uuid.UUID(int=1, version=4)),
        "snapshot_event_count": 5,
    }
    assert applied["rpc_calls"] == 1
    assert len(client.upserts) == 1
    assert client.upserts[0]["p_session_id"] == session_id
    assert client.upserts[0]["p_user_id"] == user_id
    assert client.upserts[0]["p_total_tokens"] == 220
    assert client.upserts[0]["p_model_context_window"] is None
    assert client.upserts[0]["p_observed_at"] == "2026-07-26T10:05:00Z"
    assert client.upserts[0]["p_metadata"] == {
        "source": "rollout_sync_usage_reprocess",
        "file_generation": "b" * 16,
    }


def test_reprocess_rejects_gaps_and_hash_mismatches_without_writes():
    reprocess = load_module("reprocess_rollout_usage_invalid", REPROCESS)
    session_id = "11111111-1111-4111-8111-111111111111"
    user_id = "22222222-2222-4222-8222-222222222222"
    raw = rollout_bytes(usage_envelope("2026-07-26T10:00:00Z", 100))
    events, objects = stored_generation(
        session_id,
        user_id,
        "c" * 16,
        raw,
        cuts=(20,),
    )
    events[1]["metadata"]["start_offset"] = 21
    events[1]["metadata"]["content_byte_size"] = (
        events[1]["metadata"]["end_offset"] - 21
    )
    client = FakeClient(events, objects)

    result = reprocess.reprocess_rollout_usage(client, apply=True)

    assert result["sessions_with_usage"] == 0
    assert result["rpc_calls"] == 0
    assert len(result["errors"]) == 1
    assert "non-contiguous offset" in result["errors"][0]
    assert client.upserts == []


def test_reprocess_materialized_snapshot_defers_late_low_sequence_commits():
    reprocess = load_module("reprocess_rollout_usage_mutation", REPROCESS)
    before_session = "11111111-1111-4111-8111-111111111111"
    active_session = "22222222-2222-4222-8222-222222222222"
    later_session = "44444444-4444-4444-8444-444444444444"
    after_session = "55555555-5555-4555-8555-555555555555"
    user_id = "66666666-6666-4666-8666-666666666666"
    first_events, first_objects = stored_generation(
        active_session,
        user_id,
        "a" * 16,
        rollout_bytes(usage_envelope("2026-07-26T10:00:00Z", 100)),
        cuts=(),
    )
    second_events, second_objects = stored_generation(
        later_session,
        user_id,
        "b" * 16,
        rollout_bytes(usage_envelope("2026-07-26T10:01:00Z", 200)),
        cuts=(),
    )
    before_events, before_objects = stored_generation(
        before_session,
        user_id,
        "c" * 16,
        rollout_bytes(usage_envelope("2026-07-26T10:02:00Z", 300)),
        cuts=(),
    )
    within_events, within_objects = stored_generation(
        active_session,
        user_id,
        "d" * 16,
        rollout_bytes(usage_envelope("2026-07-26T10:03:00Z", 400)),
        cuts=(),
    )
    after_events, after_objects = stored_generation(
        after_session,
        user_id,
        "e" * 16,
        rollout_bytes(usage_envelope("2026-07-26T10:04:00Z", 500)),
        cuts=(),
    )
    inserted_events = [*before_events, *within_events, *after_events]
    inserted_objects = {**before_objects, **within_objects, **after_objects}
    for event in inserted_events:
        event["created_at"] = "2020-01-01T00:00:00Z"
        event["preallocated_sequence"] = 0

    class MutatingClient(FakeClient):
        inserted = False

        def iter_rollout_events(
            self,
            *,
            page_size: int,
            after_session: str | None,
            snapshot_id: str,
        ):
            cursor = None
            yielded = 0
            while True:
                eligible = sorted(
                    (
                        row for row in self.snapshots[snapshot_id]
                        if (
                            after_session is None
                            or row["session_id"] > after_session
                        )
                        and (
                            cursor is None
                            or (
                                row["session_id"],
                                row["id"],
                            ) > cursor
                        )
                    ),
                    key=lambda row: (
                        row["session_id"],
                        row["id"],
                    ),
                )[:page_size]
                if not eligible:
                    return
                for row in eligible:
                    yield row
                    cursor = (
                        row["session_id"],
                        row["id"],
                    )
                    yielded += 1
                if yielded == page_size and not self.inserted:
                    self.insert_events(inserted_events)
                    self.objects.update(inserted_objects)
                    self.inserted = True

    client = MutatingClient(
        [*first_events, *second_events],
        {**first_objects, **second_objects},
    )

    first_result = reprocess.reprocess_rollout_usage(
        client,
        apply=True,
        page_size=1,
    )

    assert first_result["events"] == 2
    assert first_result["sessions"] == 2
    assert first_result["rpc_calls"] == 2
    assert first_result["resume_after_session"] == later_session
    assert first_result["snapshot_event_count"] == 2
    assert [call["p_session_id"] for call in client.upserts] == [
        active_session,
        later_session,
    ]

    resumed_result = reprocess.reprocess_rollout_usage(
        client,
        after_session=active_session,
        snapshot_id=first_result["snapshot_id"],
        page_size=1,
    )
    assert resumed_result["events"] == 1
    assert resumed_result["sessions"] == 1
    assert resumed_result["resume_after_session"] == later_session
    assert resumed_result["snapshot_id"] == first_result["snapshot_id"]
    assert resumed_result["snapshot_event_count"] == 2

    client.upserts.clear()
    next_result = reprocess.reprocess_rollout_usage(client, apply=True, page_size=1)

    assert next_result["events"] == 5
    assert next_result["sessions"] == 4
    assert next_result["rpc_calls"] == 4
    assert next_result["snapshot_id"] != first_result["snapshot_id"]
    assert next_result["snapshot_event_count"] == 5
    assert [call["p_session_id"] for call in client.upserts] == [
        before_session,
        active_session,
        later_session,
        after_session,
    ]


def test_supabase_replay_pages_with_keysets_not_offsets():
    reprocess = load_module("reprocess_rollout_usage_keyset", REPROCESS)
    client = reprocess.SupabaseAdminClient("https://example.supabase.co", "secret")
    paths: list[str] = []
    rows = [
        {
            "id": "00000000-0000-4000-8000-000000000001",
            "session_id": "11111111-1111-4111-8111-111111111111",
        },
        {
            "id": "00000000-0000-4000-8000-000000000002",
            "session_id": "22222222-2222-4222-8222-222222222222",
        },
    ]
    snapshot_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"

    def request_json(method: str, path: str, payload=None):
        paths.append(path)
        if path.endswith("/rpc/create_codex_rollout_replay_snapshot"):
            assert method == "POST"
            assert payload == {}
            return [{"snapshot_id": snapshot_id, "event_count": len(rows)}]
        if "/codex_rollout_replay_snapshots?" in path:
            assert method == "GET"
            assert payload is None
            return [{"event_count": len(rows)}]
        assert method == "GET"
        assert payload is None
        page_index = len([value for value in paths if "session_id.asc" in value]) - 1
        return [rows[page_index]] if page_index < len(rows) else []

    client.request_json = request_json
    created_snapshot_id, event_count = client.create_rollout_replay_snapshot()
    assert created_snapshot_id == snapshot_id
    assert event_count == 2
    assert client.rollout_replay_snapshot_event_count(snapshot_id) == 2

    assert list(client.iter_rollout_events(
        page_size=1,
        after_session=None,
        snapshot_id=snapshot_id,
    )) == rows
    page_paths = [path for path in paths if "session_id.asc" in path]
    assert len(page_paths) == 3
    assert all("offset=" not in path for path in page_paths)
    assert "or=" in page_paths[1]
    assert "session_id.gt." in page_paths[1]
    assert "id.gt.00000000-0000-4000-8000-000000000001" in page_paths[1]
    assert f"snapshot_id=eq.{snapshot_id}" in page_paths[0]


def test_usage_rpc_migration_is_monotonic_and_service_role_only():
    migrations = sorted((PLUGIN / "supabase" / "migrations").glob("*upsert_codex_session_usage_latest.sql"))
    assert len(migrations) == 1
    sql = migrations[0].read_text(encoding="utf-8").lower()

    assert "create or replace function public.upsert_codex_session_usage_latest" in sql
    assert "security invoker" in sql
    assert "set search_path = ''" in sql
    assert "excluded.total_tokens >= public.codex_session_usage.total_tokens" in sql
    assert "excluded.observed_at >= public.codex_session_usage.observed_at" in sql
    assert "create table public.codex_rollout_replay_snapshots" in sql
    assert "create table public.codex_rollout_replay_snapshot_events" in sql
    assert "primary key (snapshot_id, session_id, id)" in sql
    assert "create or replace function public.create_codex_rollout_replay_snapshot" in sql
    assert "insert into public.codex_rollout_replay_snapshot_events" in sql
    assert "from public.codex_session_events as event" in sql
    assert "where event.event_type = 'rollout_chunk'" in sql
    assert "security definer" in sql
    assert "create or replace function public.delete_codex_rollout_replay_snapshot" in sql
    assert "from public, anon, authenticated, service_role" in sql
    assert "catalog_sequence" not in sql
    assert "codex_session_usage_additive_total_check" in sql
    assert "input_tokens + cached_input_tokens + output_tokens + reasoning_output_tokens" in sql
    assert "raise exception 'usage token components must sum exactly to total_tokens'" in sql
    assert "coalesce(\n      excluded.model_context_window" in sql
    assert ") from public, anon, authenticated" in sql
    assert ") to service_role" in sql
