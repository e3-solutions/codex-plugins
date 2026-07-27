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


class FakeClient:
    def __init__(self, events: list[dict], objects: dict[tuple[str, str], bytes]):
        self.events = events
        self.objects = objects
        self.upserts: list[dict] = []
        self.queries: list[dict] = []

    def iter_rollout_events(self, **query):
        self.queries.append(query)
        rows = self.events
        if query["after_session"]:
            rows = [row for row in rows if row["session_id"] > query["after_session"]]
        yield from sorted(rows, key=lambda row: (row["session_id"], row["id"]))

    def download(self, bucket: str, storage_path: str) -> bytes:
        return self.objects[(bucket, storage_path)]

    def upsert_usage(self, parameters: dict) -> None:
        self.upserts.append(parameters)


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
