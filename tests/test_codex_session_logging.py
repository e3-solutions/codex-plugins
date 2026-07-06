from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "plugins" / "codex-session-logging" / "scripts" / "session_logging.py"


def load_session_logging():
    spec = importlib.util.spec_from_file_location("session_logging", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_queue_records(path: Path) -> list[dict]:
    pending_dir = path / "queue" / "pending"
    if pending_dir.exists():
        return [
            json.loads(record.read_text(encoding="utf-8"))
            for record in sorted(pending_dir.glob("*.json"))
        ]
    return [
        json.loads(record.read_text(encoding="utf-8"))
        for record in sorted((path / "queue").glob("*.json"))
    ]


def init_git_repo(path: Path, remote: str) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, check=True)
    return path


def test_user_prompt_submit_spools_full_prompt_and_indexes_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    session_logging = load_session_logging()
    repo = init_git_repo(tmp_path / "repo", "https://github.com/e3-solutions/codex-plugins.git")

    result = session_logging.capture_hook_event(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-123",
            "turn_id": "turn-1",
            "cwd": str(repo),
            "prompt": "Please add session logging.\nInclude exact prompts.",
            "transcript_path": str(tmp_path / "transcript.jsonl"),
        }
    )

    message_path = tmp_path / "state" / result["local_content_path"]
    message = json.loads(message_path.read_text(encoding="utf-8"))
    events = read_jsonl(tmp_path / "state" / "events.jsonl")

    assert result["role"] == "user"
    assert result["session_id"] == "session-123"
    assert result["turn_id"] == "turn-1"
    assert result["content_byte_size"] == len("Please add session logging.\nInclude exact prompts.".encode("utf-8"))
    assert result["storage_path"] == "users/local/sessions/session-123/messages/000001-user.json"
    assert message["content"] == "Please add session logging.\nInclude exact prompts."
    assert message["role"] == "user"
    assert events[0]["content_excerpt"] == "Please add session logging.\nInclude exact prompts."
    assert "content" not in events[0]
    assert events[0]["metadata"]["transcript_path"].endswith("transcript.jsonl")


def test_stop_spools_assistant_message_with_next_sequence(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    session_logging = load_session_logging()
    repo = init_git_repo(tmp_path / "repo", "git@github.com:e3-solutions/codex-plugins.git")

    session_logging.capture_hook_event(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-123",
            "turn_id": "turn-1",
            "cwd": str(repo),
            "prompt": "What changed?",
        }
    )
    result = session_logging.capture_hook_event(
        {
            "hook_event_name": "Stop",
            "session_id": "session-123",
            "turn_id": "turn-1",
            "cwd": str(repo),
            "last_assistant_message": "I added a logging plugin and tests.",
            "transcript_path": str(tmp_path / "transcript.jsonl"),
        }
    )

    message_path = tmp_path / "state" / result["local_content_path"]
    message = json.loads(message_path.read_text(encoding="utf-8"))
    events = read_jsonl(tmp_path / "state" / "events.jsonl")

    assert result["role"] == "assistant"
    assert result["seq"] == 2
    assert result["storage_path"] == "users/local/sessions/session-123/messages/000002-assistant.json"
    assert message["content"] == "I added a logging plugin and tests."
    assert events[1]["role"] == "assistant"
    assert events[1]["content_sha256"] == result["content_sha256"]


def test_capture_skips_repos_outside_e3_solutions(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    session_logging = load_session_logging()
    repo = init_git_repo(tmp_path / "repo", "https://github.com/example/codex-plugins.git")

    result = session_logging.capture_hook_event(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-123",
            "turn_id": "turn-1",
            "cwd": str(repo),
            "prompt": "Do not capture this.",
        }
    )

    assert result is None
    assert not (tmp_path / "state" / "events.jsonl").exists()


def test_capture_spawns_background_drain_without_uploading_inline(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_SESSION_LOG_SUPABASE_SERVICE_ROLE_KEY", "service-role-key")
    monkeypatch.setenv("CODEX_SESSION_LOG_USER_ID", "11111111-1111-1111-1111-111111111111")
    session_logging = load_session_logging()
    repo = init_git_repo(tmp_path / "repo", "https://github.com/e3-solutions/codex-plugins.git")
    launches = []

    def fail_inline_drain():
        raise AssertionError("capture should not upload inline")

    class RecordingProcess:
        def __init__(self, args, **kwargs):
            launches.append((args, kwargs))

    monkeypatch.setattr(session_logging, "drain_queue", fail_inline_drain)
    monkeypatch.setattr(
        session_logging,
        "git_origin_remote",
        lambda cwd: "https://github.com/e3-solutions/codex-plugins.git",
    )
    monkeypatch.setattr(session_logging.subprocess, "Popen", RecordingProcess)

    result = session_logging.capture_hook_event(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-123",
            "turn_id": "turn-1",
            "cwd": str(repo),
            "prompt": "Capture quickly.",
        }
    )

    assert result["role"] == "user"
    assert launches
    assert "drain_queue.py" in launches[0][0][1]
    assert not (tmp_path / "state" / "upload_errors.jsonl").exists()


def test_drain_uploads_records_enqueued_during_upload_before_returning(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("CODEX_SESSION_LOG_SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("CODEX_SESSION_LOG_USER_ID", raising=False)
    session_logging = load_session_logging()
    repo = init_git_repo(tmp_path / "repo", "https://github.com/e3-solutions/codex-plugins.git")

    session_logging.capture_hook_event(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-123",
            "turn_id": "turn-1",
            "cwd": str(repo),
            "prompt": "First prompt.",
        }
    )

    class CapturingUploader:
        def __init__(self):
            self.captured_second_record = False

        def upload_message(self, record, *, base):
            if not self.captured_second_record:
                self.captured_second_record = True
                session_logging.capture_hook_event(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": "session-123",
                        "turn_id": "turn-2",
                        "cwd": str(repo),
                        "prompt": "Second prompt.",
                    }
                )

    uploader = CapturingUploader()
    monkeypatch.setattr(session_logging.SupabaseUploader, "from_env", classmethod(lambda cls: uploader))

    result = session_logging.drain_queue()
    queued = read_queue_records(tmp_path / "state")

    assert result["uploaded"] == 2
    assert result["remaining"] == 0
    assert queued == []


def test_drain_remaps_local_spool_records_to_configured_user_storage_path(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    session_logging = load_session_logging()
    repo = init_git_repo(tmp_path / "repo", "https://github.com/e3-solutions/codex-plugins.git")
    user_id = "11111111-1111-1111-1111-111111111111"

    session_logging.capture_hook_event(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-123",
            "turn_id": "turn-1",
            "cwd": str(repo),
            "prompt": "Captured before user id is configured.",
        }
    )

    class RecordingUploader(session_logging.SupabaseUploader):
        def __init__(self):
            super().__init__(
                supabase_url="https://example.supabase.co",
                service_role_key="key",
                user_id=user_id,
                bucket="codex-sessions",
            )
            self.uploaded_paths = []
            self.upserts = []

        def storage_upload(self, path, content):
            self.uploaded_paths.append(path)

        def rest_upsert(self, table, row, *, conflict):
            self.upserts.append((table, row, conflict))

    uploader = RecordingUploader()
    monkeypatch.setattr(session_logging.SupabaseUploader, "from_env", classmethod(lambda cls: uploader))

    result = session_logging.drain_queue()
    message_rows = [row for table, row, conflict in uploader.upserts if table == "codex_session_messages"]

    assert result["uploaded"] == 1
    assert uploader.uploaded_paths == [f"users/{user_id}/sessions/session-123/messages/000001-user.json"]
    assert message_rows[0]["storage_path"] == f"users/{user_id}/sessions/session-123/messages/000001-user.json"


def test_upload_message_upserts_message_rows_for_idempotent_retries(tmp_path, monkeypatch):
    session_logging = load_session_logging()
    message_path = tmp_path / "users" / "local" / "sessions" / "session-123" / "messages" / "000001-user.json"
    message_path.parent.mkdir(parents=True)
    message_path.write_text('{"content": "retry me"}\n', encoding="utf-8")
    record = {
        "id": "22222222222222222222222222222222",
        "session_id": "session-123",
        "turn_id": "turn-1",
        "seq": 1,
        "role": "user",
        "storage_path": "users/local/sessions/session-123/messages/000001-user.json",
        "local_content_path": "users/local/sessions/session-123/messages/000001-user.json",
        "content_sha256": "abc123",
        "content_byte_size": 23,
        "content_excerpt": "retry me",
        "metadata": {},
        "created_at": "2026-07-03T00:00:00+00:00",
    }

    class RecordingUploader(session_logging.SupabaseUploader):
        def __init__(self):
            super().__init__(
                supabase_url="https://example.supabase.co",
                service_role_key="key",
                user_id="11111111-1111-1111-1111-111111111111",
                bucket="codex-sessions",
            )
            self.upserts = []

        def storage_upload(self, path, content):
            pass

        def rest_upsert(self, table, row, *, conflict):
            self.upserts.append((table, conflict))

        def rest_insert(self, table, row):
            raise AssertionError(f"{table} should be upserted, not inserted")

    uploader = RecordingUploader()
    uploader.upload_message(record, base=tmp_path)

    assert ("codex_session_messages", "id") in uploader.upserts


def test_plugin_packaging_and_supabase_migration_are_present():
    manifest_path = ROOT / "plugins" / "codex-session-logging" / ".codex-plugin" / "plugin.json"
    hooks_path = ROOT / "plugins" / "codex-session-logging" / "hooks" / "hooks.json"
    marketplace_path = ROOT / ".agents" / "plugins" / "marketplace.json"
    migration_path = (
        ROOT
        / "plugins"
        / "codex-session-logging"
        / "supabase"
        / "migrations"
        / "001_codex_session_logging.sql"
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
    migration = migration_path.read_text(encoding="utf-8")
    all_migrations = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "plugins/codex-session-logging/supabase/migrations").glob("*.sql"))
    )

    assert manifest["name"] == "codex-session-logging"
    assert manifest["hooks"] == "./hooks/hooks.json"
    assert hooks_path.exists()
    assert "UserPromptSubmit" in hooks["hooks"]
    assert "Stop" in hooks["hooks"]
    assert any(plugin["name"] == "codex-session-logging" for plugin in marketplace["plugins"])
    assert "create table if not exists public.codex_sessions" in migration
    assert "create table if not exists public.codex_session_messages" in migration
    assert "create table if not exists public.codex_session_events" in migration
    assert "alter table public.codex_sessions enable row level security" in migration
    assert "revoke all privileges on public.codex_sessions from authenticated" in all_migrations
    assert "grant select on public.codex_sessions to authenticated" in all_migrations
    assert "grant select, insert, update on public.codex_sessions to authenticated" not in all_migrations
    assert "codex-sessions" in migration
    assert "storage.objects" in migration
