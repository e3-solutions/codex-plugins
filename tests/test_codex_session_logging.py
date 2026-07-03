from __future__ import annotations

import importlib.util
import json
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


def test_user_prompt_submit_spools_full_prompt_and_indexes_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    session_logging = load_session_logging()

    result = session_logging.capture_hook_event(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-123",
            "turn_id": "turn-1",
            "cwd": str(tmp_path / "repo"),
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

    session_logging.capture_hook_event(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-123",
            "turn_id": "turn-1",
            "prompt": "What changed?",
        }
    )
    result = session_logging.capture_hook_event(
        {
            "hook_event_name": "Stop",
            "session_id": "session-123",
            "turn_id": "turn-1",
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

    assert manifest["name"] == "codex-session-logging"
    assert hooks_path.exists()
    assert "UserPromptSubmit" in hooks["hooks"]
    assert "Stop" in hooks["hooks"]
    assert any(plugin["name"] == "codex-session-logging" for plugin in marketplace["plugins"])
    assert "create table if not exists public.codex_sessions" in migration
    assert "create table if not exists public.codex_session_messages" in migration
    assert "create table if not exists public.codex_session_events" in migration
    assert "alter table public.codex_sessions enable row level security" in migration
    assert "codex-sessions" in migration
    assert "storage.objects" in migration
