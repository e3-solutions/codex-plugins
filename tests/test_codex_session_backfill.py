from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-session-logging" / "scripts"


def load_backfill():
    sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("backfill_sessions", SCRIPTS / "backfill_sessions.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_transcript(path: Path, *, remote: str, include_usage: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "timestamp": "2025-01-02T03:04:05.000Z",
            "type": "session_meta",
            "payload": {
                "id": "019f4d80-41da-79a3-9120-804bdf740ce0",
                "session_id": "019f4d80-41da-79a3-9120-804bdf740ce0",
                "cwd": "/deleted/checkout",
                "git": {"repository_url": remote, "commit_hash": "abc123"},
            },
        },
        {
            "timestamp": "2025-01-02T03:04:06.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Historical prompt"}],
            },
        },
        {
            "timestamp": "2025-01-02T03:04:07.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Historical prompt"},
        },
        {
            "timestamp": "2025-01-02T03:04:07.500Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "commentary",
                "content": [{"type": "output_text", "text": "Intermediate commentary"}],
            },
        },
        {
            "timestamp": "2025-01-02T03:04:08.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "phase": "final_answer",
                "content": [{"type": "output_text", "text": "Historical answer"}],
            },
        },
    ]
    if include_usage:
        rows.append({
            "timestamp": "2025-01-02T03:04:08.500Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 4090,
                        "cached_input_tokens": 1024,
                        "output_tokens": 52,
                        "reasoning_output_tokens": 8,
                        "total_tokens": 4142,
                    },
                    "model_context_window": 258400,
                },
            },
        })
    rows.append({
            "timestamp": "2025-01-02T03:04:09.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "Do not upload"}],
            },
        })
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_backfill_queues_e3_messages_with_stable_ids_and_original_timestamps(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_SESSION_LOG_AUTO_UPLOAD", "0")
    transcript = tmp_path / "codex" / "sessions" / "2025" / "01" / "02" / "rollout.jsonl"
    write_transcript(transcript, remote="git@github.com:e3-solutions/old-repo.git")
    backfill = load_backfill()

    first = backfill.run_backfill(max_files=10)
    queue = sorted((tmp_path / "state" / "queue" / "pending").glob("*.json"))
    records = [json.loads(path.read_text(encoding="utf-8")) for path in queue]
    second = backfill.run_backfill(max_files=10)

    assert first["queued"] == 2
    assert first["status"] == "partial"
    assert second["processed"] == 0
    assert len(queue) == 2
    assert {record["role"] for record in records} == {"user", "assistant"}
    assert all(record["seq"] < 0 for record in records)
    assert all(record["created_at"].startswith("2025-01-02") for record in records)
    assert all(record["metadata"]["repo_remote"] == "git@github.com:e3-solutions/old-repo.git" for record in records)
    assert all(record["metadata"]["source"] == "historical_transcript" for record in records)

    payload = backfill.build_ingest_payload(records[0], base=tmp_path / "state")
    assert payload["client"]["repo_remote"] == "https://github.com/e3-solutions/old-repo.git"
    assert payload["message"]["content"] in {"Historical prompt", "Historical answer"}


def test_auto_upload_disabled_does_not_report_status(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_SESSION_LOG_AUTO_UPLOAD", "0")
    transcript = tmp_path / "codex" / "sessions" / "rollout.jsonl"
    write_transcript(transcript, remote="https://github.com/e3-solutions/repo.git")
    backfill = load_backfill()
    monkeypatch.setattr(
        backfill,
        "report_status",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not report")),
    )

    result = backfill.run_backfill(max_files=10)
    state = backfill.read_state(tmp_path / "state")

    assert result["status"] == "partial"
    assert state["completed_at"] is None
    assert state["last_drain"]["disabled"] is True


def test_backfill_queues_final_cumulative_token_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_SESSION_LOG_AUTO_UPLOAD", "0")
    transcript = tmp_path / "codex" / "sessions" / "usage.jsonl"
    write_transcript(
        transcript,
        remote="https://github.com/e3-solutions/repo.git",
        include_usage=True,
    )
    backfill = load_backfill()

    result = backfill.run_backfill(max_files=10)
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / "state" / "queue" / "pending").glob("*.json")
    ]
    usage_record = next(record for record in records if record.get("type") == "usage")
    payload = backfill.build_ingest_payload(usage_record, base=tmp_path / "state")

    assert result["queued"] == 3
    assert payload["usage"]["input_tokens"] == 4090
    assert payload["usage"]["cached_input_tokens"] == 1024
    assert payload["usage"]["output_tokens"] == 52
    assert payload["usage"]["reasoning_output_tokens"] == 8
    assert payload["usage"]["total_tokens"] == 4142
    assert payload["usage"]["model_context_window"] == 258400


def test_existing_message_checkpoint_queues_only_missing_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_SESSION_LOG_AUTO_UPLOAD", "0")
    transcript = tmp_path / "codex" / "sessions" / "usage-upgrade.jsonl"
    write_transcript(
        transcript,
        remote="https://github.com/e3-solutions/repo.git",
        include_usage=True,
    )
    backfill = load_backfill()
    base = tmp_path / "state"
    backfill.write_json_atomic(
        backfill.state_path(base),
        {
            "version": 1,
            "files": {
                str(transcript): {
                    "fingerprint": backfill.file_fingerprint(transcript),
                    "status": "complete",
                    "queued": 2,
                },
            },
        },
    )

    result = backfill.run_backfill(max_files=10)
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (base / "queue" / "pending").glob("*.json")
    ]
    state = backfill.read_state(base)

    assert result["queued"] == 1
    assert [record["type"] for record in records] == ["usage"]
    assert state["files"][str(transcript)]["queued"] == 3
    assert state["files"][str(transcript)]["usage_version"] == 1


def test_legacy_response_user_is_used_when_canonical_user_event_is_absent():
    backfill = load_backfill()
    envelope = {
        "timestamp": "2024-01-01T00:00:00Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Legacy prompt"}],
        },
    }

    assert backfill.historical_message(envelope) == (
        "user",
        "Legacy prompt",
        None,
        "2024-01-01T00:00:00Z",
    )


def test_historical_sequences_do_not_collide_across_resumed_transcript_files(tmp_path):
    backfill = load_backfill()

    first = backfill.historical_sequence(tmp_path / "rollout-a.jsonl", 42)
    second = backfill.historical_sequence(tmp_path / "rollout-b.jsonl", 42)

    assert first < 0
    assert second < 0
    assert first != second


def test_mixed_format_only_skips_response_users_in_canonical_turn(tmp_path):
    backfill = load_backfill()
    transcript = tmp_path / "mixed.jsonl"
    rows = [
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": "Legacy prompt"},
        },
        {"type": "event_msg", "payload": {"type": "task_started"}},
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": "Injected context"},
        },
        {
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": "Modern prompt"},
        },
        {"type": "event_msg", "payload": {"type": "user_message", "message": "Modern prompt"}},
    ]
    transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    skipped = backfill.response_user_lines_to_skip(transcript)

    assert 0 not in skipped
    assert skipped == {2, 3}


def test_locked_drain_is_reconciled_before_reporting_complete(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_SESSION_LOG_AUTO_UPLOAD", "1")
    monkeypatch.setenv("CODEX_SESSION_LOG_BACKFILL_HEARTBEAT_SECONDS", "0")
    transcript = tmp_path / "codex" / "sessions" / "rollout.jsonl"
    write_transcript(transcript, remote="https://github.com/e3-solutions/repo.git")
    backfill = load_backfill()
    drain_calls = []
    reports = []
    pre_drain_states = []

    def fake_drain(progress_callback=None):
        drain_calls.append(True)
        pre_drain_states.append(backfill.read_state(tmp_path / "state"))
        if len(drain_calls) == 1:
            return {"uploaded": 0, "failed": 0, "dead_lettered": 0, "remaining": 0, "locked": True}
        for path in (tmp_path / "state" / "queue" / "pending").glob("*.json"):
            path.unlink()
        if progress_callback:
            progress_callback({"uploaded": 2, "failed": 0, "dead_lettered": 0, "remaining": 0})
        return {"uploaded": 2, "failed": 0, "dead_lettered": 0, "remaining": 0}

    monkeypatch.setattr(backfill, "drain_queue", fake_drain)
    monkeypatch.setattr(backfill.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(backfill, "report_status", lambda state, **kwargs: reports.append(state.copy()))

    result = backfill.run_backfill(max_files=10)

    assert len(drain_calls) == 2
    assert all(state["status"] == "running" for state in pre_drain_states)
    assert all(state["completed_at"] is None for state in pre_drain_states)
    assert result["status"] == "complete"
    assert any(report["status"] == "running" for report in reports)
    assert any(report.get("last_drain", {}).get("uploaded") == 2 for report in reports)
    assert reports[-1]["status"] == "complete"
    assert reports[-1]["completed_at"] is not None


def test_backfill_skips_non_e3_transcripts_without_queuing_content(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_SESSION_LOG_AUTO_UPLOAD", "0")
    transcript = tmp_path / "codex" / "sessions" / "rollout.jsonl"
    write_transcript(transcript, remote="https://github.com/another-org/private.git")
    backfill = load_backfill()

    result = backfill.run_backfill(max_files=10)

    assert result["queued"] == 0
    assert result["skipped_non_e3"] == 1
    assert not (tmp_path / "state" / "queue" / "pending").exists()


def test_backfill_database_migration_is_private_and_service_role_writable():
    migration = (
        ROOT
        / "plugins/codex-session-logging/supabase/migrations/20260710193433_codex_session_backfill_runs.sql"
    ).read_text(encoding="utf-8")

    assert "create table if not exists public.codex_session_backfill_runs" in migration
    assert "alter table public.codex_session_backfill_runs enable row level security" in migration
    assert "using ((select auth.uid()) = user_id)" in migration
    assert "grant select on public.codex_session_backfill_runs to authenticated" in migration
    assert "grant select, insert, update on public.codex_session_backfill_runs to service_role" in migration

    usage_migration = (
        ROOT
        / "plugins/codex-session-logging/supabase/migrations/20260710210437_historical_session_usage.sql"
    ).read_text(encoding="utf-8")
    assert "create table public.codex_session_usage" in usage_migration
    assert "references public.codex_sessions(id) on delete cascade" in usage_migration
    assert "alter table public.codex_session_usage enable row level security" in usage_migration
    assert "using ((select auth.uid()) = user_id)" in usage_migration
    assert "grant select on public.codex_session_usage to authenticated" in usage_migration
    assert "grant select, insert, update on public.codex_session_usage to service_role" in usage_migration


def test_session_start_does_not_spawn_historical_backfills():
    source = (SCRIPTS / "session_start.py").read_text(encoding="utf-8")

    assert "spawn_backfill" not in source
    assert "backfill_sessions.py" not in source
    assert "CODEX_SESSION_LOG_BACKFILL" not in source
