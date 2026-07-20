from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "plugins" / "claude-session-logging" / "scripts"
MODULE_PATH = SCRIPTS_DIR / "session_logging.py"
UPDATE_MODULE_PATH = SCRIPTS_DIR / "update_plugin.py"


def _load_named(name: str):
    """Load a claude-session-logging script by path, registering it under its
    real name so sibling ``import`` statements resolve — without mutating
    ``sys.path`` (which would leak into the codex test modules)."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_scripts_module(name: str):
    """Load a claude script and its intra-plugin dependencies in the right order
    so each ``import session_logging`` / ``import publish_presence`` resolves to
    the claude-session-logging copy."""
    session_logging = _load_named("session_logging")
    if name == "session_logging":
        return session_logging
    if name in {"transcript_sync", "backfill_sessions"}:
        _load_named("publish_presence")
    if name == "transcript_sync":
        return _load_named("transcript_sync")
    if name == "backfill_sessions":
        _load_named("transcript_sync")
    return _load_named(name)


@pytest.fixture(autouse=True)
def disable_background_uploads_by_default(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_LOG_AUTO_UPLOAD", "0")


@pytest.fixture(autouse=True)
def restore_shared_script_modules():
    # These claude scripts register themselves in sys.modules under the same
    # bare names the codex-session-logging test loaders use ("session_logging",
    # etc.). Clear them on teardown so the codex tests re-import their own copies
    # instead of reusing whatever this file cached.
    shared = ("session_logging", "publish_presence", "backfill_sessions", "presence_ticker", "transcript_sync")
    yield
    for name in shared:
        sys.modules.pop(name, None)


def load_session_logging():
    spec = importlib.util.spec_from_file_location("claude_session_logging", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_update_plugin():
    spec = importlib.util.spec_from_file_location("claude_session_logging_updater", UPDATE_MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def init_git_repo(path: Path, remote: str) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, check=True)
    return path


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_session_start_tracks_claude_thread_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    session_logging = load_session_logging()
    repo = init_git_repo(tmp_path / "repo", "https://github.com/e3-solutions/codex-plugins.git")

    result = session_logging.capture_hook_event(
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session-123",
            "cwd": str(repo),
            "transcript_path": str(tmp_path / "transcript.jsonl"),
            "source": "startup",
            "permission_mode": "default",
        }
    )

    detail = json.loads((tmp_path / "state" / result["local_content_path"]).read_text(encoding="utf-8"))
    events = read_jsonl(tmp_path / "state" / "events.jsonl")

    assert result["type"] == "event"
    assert result["event_type"] == "thread_started"
    assert result["session_id"] == "claude-session-123"
    assert result["storage_path"] == "users/local/sessions/claude-session-123/events/000001-thread_started.json"
    assert detail["metadata"] == {
        "agent": "claude",
        "cwd": str(repo),
        "permission_mode": "default",
        "platform": "claude-code",
        "source": "startup",
        "thread_event": "started",
        "transcript_path": str(tmp_path / "transcript.jsonl"),
    }
    assert events[0]["metadata"]["platform"] == "claude-code"


def test_user_prompt_submit_tracks_turn_boundary_without_prompt_text(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    session_logging = load_session_logging()
    repo = init_git_repo(tmp_path / "repo", "git@github.com:e3-solutions/codex-plugins.git")
    prompt = "refactor auth using secret-token-should-not-store"

    result = session_logging.capture_hook_event(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "claude-session-123",
            "cwd": str(repo),
            "permission_mode": "acceptEdits",
            "prompt": prompt,
        }
    )

    detail = json.loads((tmp_path / "state" / result["local_content_path"]).read_text(encoding="utf-8"))
    detail_text = json.dumps(detail, sort_keys=True)

    assert result["event_type"] == "thread_prompt_submitted"
    assert detail["metadata"]["thread_event"] == "prompt_submitted"
    assert detail["metadata"]["prompt_byte_size"] == len(prompt.encode("utf-8"))
    assert detail["metadata"]["prompt_sha256"] == session_logging.sha256_hex(prompt)
    assert "secret-token-should-not-store" not in detail_text
    assert "tool_input" not in detail_text


def test_pre_tool_use_records_only_tool_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    session_logging = load_session_logging()
    repo = init_git_repo(tmp_path / "repo", "https://github.com/e3-solutions/codex-plugins.git")

    result = session_logging.capture_hook_event(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "claude-tools",
            "cwd": str(repo),
            "tool_name": "Bash",
            "tool_call_id": "call-1",
            "tool_input": {"command": "echo should-not-store"},
        }
    )

    detail = json.loads((tmp_path / "state" / result["local_content_path"]).read_text(encoding="utf-8"))
    detail_text = json.dumps(detail, sort_keys=True)

    assert result["event_type"] == "tool_call_started"
    assert detail["metadata"] == {
        "agent": "claude",
        "cwd": str(repo),
        "platform": "claude-code",
        "tool_call_id": "call-1",
        "tool_name": "Bash",
        "tool_phase": "started",
    }
    assert "tool_input" not in detail_text
    assert "should-not-store" not in detail_text


def test_post_tool_use_failure_records_failure_without_output(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    session_logging = load_session_logging()
    repo = init_git_repo(tmp_path / "repo", "https://github.com/e3-solutions/codex-plugins.git")

    result = session_logging.capture_hook_event(
        {
            "hook_event_name": "PostToolUseFailure",
            "session_id": "claude-tools",
            "cwd": str(repo),
            "tool": {"name": "Read"},
            "error": "secret failure body should not store",
        }
    )

    detail = json.loads((tmp_path / "state" / result["local_content_path"]).read_text(encoding="utf-8"))
    detail_text = json.dumps(detail, sort_keys=True)

    assert result["event_type"] == "tool_call_failed"
    assert detail["metadata"] == {
        "agent": "claude",
        "cwd": str(repo),
        "platform": "claude-code",
        "success": False,
        "tool_name": "Read",
        "tool_phase": "failed",
    }
    assert "secret failure body" not in detail_text


def test_permission_denied_records_tool_metadata_without_inputs(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    session_logging = load_session_logging()
    repo = init_git_repo(tmp_path / "repo", "https://github.com/e3-solutions/codex-plugins.git")

    result = session_logging.capture_hook_event(
        {
            "hook_event_name": "PermissionDenied",
            "session_id": "claude-tools",
            "cwd": str(repo),
            "tool_name": "Bash",
            "tool_call_id": "call-denied",
            "tool_input": {"command": "cat secret-file"},
            "reason": "policy",
        }
    )

    detail = json.loads((tmp_path / "state" / result["local_content_path"]).read_text(encoding="utf-8"))
    detail_text = json.dumps(detail, sort_keys=True)

    assert result["event_type"] == "tool_permission_denied"
    assert detail["metadata"] == {
        "agent": "claude",
        "cwd": str(repo),
        "platform": "claude-code",
        "tool_call_id": "call-denied",
        "tool_name": "Bash",
        "tool_phase": "permission_denied",
    }
    assert "tool_input" not in detail_text
    assert "secret-file" not in detail_text


def test_post_tool_batch_records_count_without_result_payloads(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    session_logging = load_session_logging()
    repo = init_git_repo(tmp_path / "repo", "ssh://git@github.com/e3-solutions/codex-plugins.git")

    result = session_logging.capture_hook_event(
        {
            "hook_event_name": "PostToolBatch",
            "session_id": "claude-tools",
            "cwd": str(repo),
            "tool_results": [
                {"tool_name": "Read", "content": "file secret should not store"},
                {"tool_name": "Bash", "content": "output secret should not store"},
            ],
        }
    )

    detail = json.loads((tmp_path / "state" / result["local_content_path"]).read_text(encoding="utf-8"))
    detail_text = json.dumps(detail, sort_keys=True)

    assert result["event_type"] == "tool_batch_finished"
    assert detail["metadata"]["tool_batch_size"] == 2
    assert "file secret" not in detail_text
    assert "output secret" not in detail_text


def test_capture_skips_repos_outside_e3_solutions(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    session_logging = load_session_logging()
    repo = init_git_repo(tmp_path / "repo", "https://github.com/example/codex-plugins.git")

    result = session_logging.capture_hook_event(
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session-123",
            "cwd": str(repo),
            "source": "startup",
        }
    )

    assert result is None
    assert not (tmp_path / "state" / "events.jsonl").exists()


def test_drain_posts_claude_event_payload_to_shared_ingest(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_SESSION_LOG_INGEST_URL", "https://logs.example.test/ingest")
    session_logging = load_session_logging()
    repo = init_git_repo(tmp_path / "repo", "https://github.com/e3-solutions/codex-plugins.git")

    session_logging.capture_hook_event(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "claude-tools",
            "cwd": str(repo),
            "tool_name": "Bash",
            "tool_input": {"command": "echo should-not-upload"},
        }
    )

    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            raise AssertionError("successful uploads must not read the response body")

    monkeypatch.setattr(session_logging.urllib.request, "urlopen", lambda request, timeout: requests.append(request) or Response())

    result = session_logging.drain_queue()
    body = json.loads(requests[0].data.decode("utf-8"))
    body_text = json.dumps(body, sort_keys=True)

    assert result["uploaded"] == 1
    assert requests[0].full_url == "https://logs.example.test/ingest"
    assert requests[0].headers["User-agent"] == "claude-session-logging/git"
    assert body["plugin"] == {
        "name": "claude-session-logging",
        "version": "git",
    }
    assert body["record"]["type"] == "event"
    assert body["event"]["metadata"]["platform"] == "claude-code"
    assert body["client"]["repo_remote"] == "https://github.com/e3-solutions/codex-plugins.git"
    assert "should-not-upload" not in body_text


def test_claude_plugin_packaging_and_internal_marketplace_are_present():
    plugin_root = ROOT / "plugins" / "claude-session-logging"
    manifest_path = plugin_root / ".claude-plugin" / "plugin.json"
    hooks_path = plugin_root / "hooks" / "hooks.json"
    marketplace_path = ROOT / ".claude-plugin" / "marketplace.json"

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    marketplace = json.loads(marketplace_path.read_text(encoding="utf-8"))
    hooks_text = json.dumps(hooks)

    assert manifest["name"] == "claude-session-logging"
    assert "version" not in manifest
    assert "Codex" not in manifest["description"]
    assert "SessionStart" in hooks["hooks"]
    assert "UserPromptSubmit" in hooks["hooks"]
    assert "PreToolUse" in hooks["hooks"]
    assert "PostToolUse" in hooks["hooks"]
    assert "PostToolUseFailure" in hooks["hooks"]
    assert "PostToolBatch" in hooks["hooks"]
    assert "Stop" in hooks["hooks"]
    assert "StopFailure" in hooks["hooks"]
    assert "SessionEnd" in hooks["hooks"]
    assert "PermissionRequest" in hooks["hooks"]
    assert "PermissionDenied" in hooks["hooks"]
    assert "MessageDisplay" not in hooks["hooks"]
    for event_entries in hooks["hooks"].values():
        for entry in event_entries:
            for hook in entry["hooks"]:
                assert hook["command"] == "python3"
                assert "args" in hook
                assert hook["args"][0].startswith("${CLAUDE_PLUGIN_ROOT}/scripts/")
    assert "${CLAUDE_PLUGIN_ROOT}/scripts/session_start.py" in hooks_text
    assert "${CLAUDE_PLUGIN_ROOT}/scripts/pre_tool_use.py" in hooks_text
    assert "${CLAUDE_PLUGIN_ROOT}/scripts/permission_request.py" in hooks_text
    assert "${CLAUDE_PLUGIN_ROOT}/scripts/permission_denied.py" in hooks_text
    assert marketplace["name"] == "coreedge-internal"
    plugin_entry = next(plugin for plugin in marketplace["plugins"] if plugin["name"] == "claude-session-logging")
    assert plugin_entry["source"] == "./plugins/claude-session-logging"
    assert plugin_entry["category"] == "productivity"


def test_stop_event_marks_session_ended(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    session_logging = load_session_logging()
    repo = init_git_repo(tmp_path / "repo", "https://github.com/e3-solutions/codex-plugins.git")

    result = session_logging.capture_hook_event(
        {
            "hook_event_name": "Stop",
            "session_id": "claude-session-123",
            "cwd": str(repo),
        }
    )

    assert result["event_type"] == "thread_stopped"
    assert result["ended_at"] == result["created_at"]
    # Ordinary (non-end) events must not carry ended_at, so a resumed session
    # clears it again at the ingest.
    tool = session_logging.capture_hook_event(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "claude-session-123",
            "cwd": str(repo),
            "tool_name": "Bash",
        }
    )
    assert "ended_at" not in tool


def test_presence_publishes_open_e3_session(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_SESSION_LOG_INGEST_URL", "https://logs.example.test/ingest")
    monkeypatch.setenv("CLAUDE_SESSION_LOG_AUTO_UPLOAD", "1")
    monkeypatch.setenv("CLAUDE_SESSION_LOG_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("CLAUDE_SESSION_LOG_PRESENCE_STATE", str(tmp_path / "presence.json"))

    session_logging = load_scripts_module("session_logging")
    publish_presence = load_scripts_module("publish_presence")

    repo = init_git_repo(tmp_path / "repo", "https://github.com/e3-solutions/codex-plugins.git")
    slug_dir = tmp_path / "projects" / "-slug"
    slug_dir.mkdir(parents=True)
    transcript = slug_dir / "claude-open-session.jsonl"
    transcript.write_text(
        json.dumps({"type": "user", "cwd": str(repo), "gitBranch": "main", "timestamp": "2026-07-16T00:00:00Z"}) + "\n",
        encoding="utf-8",
    )

    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            raise AssertionError("successful uploads must not read the response body")

    monkeypatch.setattr(
        session_logging.urllib.request,
        "urlopen",
        lambda request, timeout: requests.append(request) or Response(),
    )

    result = publish_presence.run_presence()
    assert result["published"] == 1
    body = json.loads(requests[0].data.decode("utf-8"))
    assert body["record"]["type"] == "event"
    assert body["record"]["event_type"] == "resident_presence"
    assert body["event"]["metadata"]["agent"] == "claude"
    assert body["event"]["metadata"]["source"] == "resident_presence"
    assert body["client"]["repo_remote"] == "https://github.com/e3-solutions/codex-plugins.git"


def test_presence_skips_sessions_outside_e3(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_SESSION_LOG_INGEST_URL", "https://logs.example.test/ingest")
    monkeypatch.setenv("CLAUDE_SESSION_LOG_AUTO_UPLOAD", "1")
    monkeypatch.setenv("CLAUDE_SESSION_LOG_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("CLAUDE_SESSION_LOG_PRESENCE_STATE", str(tmp_path / "presence.json"))

    publish_presence = load_scripts_module("publish_presence")

    repo = init_git_repo(tmp_path / "repo", "https://github.com/example/other.git")
    slug_dir = tmp_path / "projects" / "-slug"
    slug_dir.mkdir(parents=True)
    transcript = slug_dir / "outside.jsonl"
    transcript.write_text(json.dumps({"cwd": str(repo), "timestamp": "2026-07-16T00:00:00Z"}) + "\n", encoding="utf-8")

    result = publish_presence.run_presence()
    assert result["published"] == 0


def test_backfill_is_idempotent_and_agent_tagged(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_SESSION_LOG_INGEST_URL", "https://logs.example.test/ingest")
    monkeypatch.setenv("CLAUDE_SESSION_LOG_AUTO_UPLOAD", "1")
    monkeypatch.setenv("CLAUDE_SESSION_LOG_PROJECTS_DIR", str(tmp_path / "projects"))

    session_logging = load_scripts_module("session_logging")
    load_scripts_module("publish_presence")
    backfill = load_scripts_module("backfill_sessions")

    repo = init_git_repo(tmp_path / "repo", "https://github.com/e3-solutions/codex-plugins.git")
    slug_dir = tmp_path / "projects" / "-slug"
    slug_dir.mkdir(parents=True)
    transcript = slug_dir / "hist-session.jsonl"
    transcript.write_text(
        json.dumps({"cwd": str(repo), "gitBranch": "main", "timestamp": "2026-07-16T00:00:00Z"}) + "\n"
        + json.dumps({"cwd": str(repo), "timestamp": "2026-07-16T00:05:00Z"}) + "\n",
        encoding="utf-8",
    )

    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b""

    monkeypatch.setattr(
        session_logging.urllib.request,
        "urlopen",
        lambda request, timeout: requests.append(request) or Response(),
    )

    first = backfill.run_backfill()
    assert first["processed"] == 1
    assert first["queued"] == 2
    bodies = [json.loads(request.data.decode("utf-8")) for request in requests]
    event_types = {body["record"]["event_type"] for body in bodies}
    assert event_types == {"thread_started", "thread_ended"}
    assert all(body["event"]["metadata"]["agent"] == "claude" for body in bodies)
    assert all(body["event"]["metadata"]["source"] == "historical_transcript" for body in bodies)

    # Re-running the same unchanged transcript enqueues nothing new.
    second = backfill.run_backfill()
    assert second["processed"] == 0


def test_auto_update_spawns_background_process(tmp_path, monkeypatch):
    update_plugin = load_update_plugin()
    calls = []

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return object()

    monkeypatch.setattr(update_plugin.subprocess, "Popen", fake_popen)
    state_path = tmp_path / "marketplace-update.json"

    result = update_plugin.maybe_spawn_auto_update(state_path=state_path)

    assert result == {"spawned": True}
    args, kwargs = calls[0]
    assert args[0] == sys.executable
    assert args[1] == str(UPDATE_MODULE_PATH)
    assert args[2:] == ["--state-path", str(state_path)]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["start_new_session"] is True


def test_auto_update_respects_opt_out(tmp_path, monkeypatch):
    update_plugin = load_update_plugin()
    monkeypatch.setenv("CLAUDE_SESSION_LOG_AUTO_UPDATE", "0")
    monkeypatch.setattr(update_plugin.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("must not spawn"))

    assert update_plugin.maybe_spawn_auto_update(state_path=tmp_path / "marketplace-update.json") == {
        "spawned": False,
        "reason": "disabled",
    }


def test_auto_update_refreshes_marketplace_then_updates_plugin(tmp_path, monkeypatch):
    update_plugin = load_update_plugin()
    commands = []

    def fake_run(args, **kwargs):
        commands.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(update_plugin.subprocess, "run", fake_run)
    state_path = tmp_path / "marketplace-update.json"

    result = update_plugin.run_update(state_path=state_path)

    assert result == {
        "updated": True,
        "marketplace_exit_code": 0,
        "plugin_exit_code": 0,
        "plugin_command": "update",
    }
    assert [args for args, _kwargs in commands] == [
        ["claude", "plugin", "marketplace", "update", "coreedge-internal"],
        ["claude", "plugin", "update", "--help"],
        ["claude", "plugin", "update", "claude-session-logging@coreedge-internal"],
    ]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["marketplace_exit_code"] == 0
    assert state["plugin_exit_code"] == 0
    assert state["plugin_command"] == "update"


def test_auto_update_falls_back_to_install_for_older_claude_code(tmp_path, monkeypatch):
    update_plugin = load_update_plugin()
    commands = []

    def fake_run(args, **kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args, 1 if args[2:4] == ["update", "--help"] else 0)

    monkeypatch.setattr(update_plugin.subprocess, "run", fake_run)

    result = update_plugin.run_update(state_path=tmp_path / "marketplace-update.json")

    assert result["updated"] is True
    assert commands[-1] == ["claude", "plugin", "install", "claude-session-logging@coreedge-internal"]


def _write_claude_transcript(path: Path, repo: Path) -> None:
    """A minimal Claude Code transcript: a user prompt, an assistant turn with
    token usage + a tool_use, and a tool_result (user-role) turn."""
    lines = [
        {
            "type": "user",
            "uuid": "line-user-1",
            "timestamp": "2026-07-16T00:00:00.000Z",
            "cwd": str(repo),
            "gitBranch": "main",
            "sessionId": "claude-xcript",
            "message": {"role": "user", "content": "Please refactor the auth module"},
        },
        {
            "type": "assistant",
            "uuid": "line-asst-1",
            "timestamp": "2026-07-16T00:00:05.000Z",
            "cwd": str(repo),
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-8",
                "content": [
                    {"type": "text", "text": "On it — reading the file first."},
                    {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {"file_path": "/auth.py"}},
                ],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 30,
                    "cache_creation_input_tokens": 10,
                    "service_tier": "standard",
                },
            },
        },
        {
            "type": "user",
            "uuid": "line-tool-1",
            "timestamp": "2026-07-16T00:00:06.000Z",
            "cwd": str(repo),
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tool-1", "content": "def login(): ..."},
                ],
            },
        },
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def _pending_records(session_logging, base: Path) -> list[dict]:
    return [session_logging.read_json_file(p) for p in session_logging.pending_queue_paths(base)]


def test_transcript_sync_emits_messages_and_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    session_logging = load_scripts_module("session_logging")
    load_scripts_module("publish_presence")
    transcript_sync = load_scripts_module("transcript_sync")

    repo = init_git_repo(tmp_path / "repo", "https://github.com/e3-solutions/codex-plugins.git")
    transcript = tmp_path / "projects" / "-slug" / "claude-xcript.jsonl"
    transcript.parent.mkdir(parents=True)
    _write_claude_transcript(transcript, repo)

    result = transcript_sync.sync_transcript_records("claude-xcript", transcript)
    assert result["messages"] == 3
    assert result["usage"] == 1

    base = tmp_path / "state"
    records = _pending_records(session_logging, base)
    messages = [r for r in records if r["type"] == "message"]
    usages = [r for r in records if r["type"] == "usage"]

    assert {m["role"] for m in messages} == {"user", "assistant", "tool"}
    assert all(m["metadata"]["agent"] == "claude" for m in messages)

    # The plain-string user prompt hashes/sizes to its exact bytes.
    user_msg = next(m for m in messages if m["role"] == "user")
    prompt = "Please refactor the auth module"
    assert user_msg["content_sha256"] == session_logging.sha256_hex(prompt)
    assert user_msg["content_byte_size"] == len(prompt.encode("utf-8"))

    # Message bodies (full parity) round-trip through build_ingest_payload and
    # the ingest's hash check would pass (byte size matches the stored content).
    for record in messages:
        payload = session_logging.build_ingest_payload(record, base=base)
        session_logging.validate_ingest_payload(payload)
        stored = payload["message"]
        assert stored["content_sha256"] == record["content_sha256"]
        assert len(stored["content"].encode("utf-8")) == record["content_byte_size"]

    # The assistant turn's tool_use is serialized into the stored body (parity).
    assistant_msg = next(m for m in messages if m["role"] == "assistant")
    assistant_body = session_logging.build_ingest_payload(assistant_msg, base=base)["message"]["content"]
    assert "tool_use" in assistant_body and "Read" in assistant_body

    assert len(usages) == 1
    usage_payload = session_logging.build_ingest_payload(usages[0], base=base)
    session_logging.validate_ingest_payload(usage_payload)
    usage = usage_payload["usage"]
    assert usage["input_tokens"] == 100
    assert usage["cached_input_tokens"] == 30
    assert usage["output_tokens"] == 20
    assert usage["reasoning_output_tokens"] == 0
    assert usage["total_tokens"] == 100 + 20 + 10 + 30
    assert usage["model_context_window"] == 200000
    assert usage["created_at"] == "2026-07-16T00:00:05.000Z"
    assert usages[0]["metadata"]["agent"] == "claude"
    assert usages[0]["metadata"]["cache_creation_input_tokens"] == 10
    assert usages[0]["metadata"]["model"] == "claude-opus-4-8"
    assert usages[0]["metadata"]["service_tier"] == "standard"


def test_transcript_sync_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    session_logging = load_scripts_module("session_logging")
    load_scripts_module("publish_presence")
    transcript_sync = load_scripts_module("transcript_sync")

    repo = init_git_repo(tmp_path / "repo", "https://github.com/e3-solutions/codex-plugins.git")
    transcript = tmp_path / "projects" / "-slug" / "claude-xcript.jsonl"
    transcript.parent.mkdir(parents=True)
    _write_claude_transcript(transcript, repo)

    base = tmp_path / "state"
    first = transcript_sync.sync_transcript_records("claude-xcript", transcript)
    assert first["queued"] == 4
    first_ids = {r["id"] for r in _pending_records(session_logging, base)}

    second = transcript_sync.sync_transcript_records("claude-xcript", transcript)
    assert second["queued"] == 0
    second_ids = {r["id"] for r in _pending_records(session_logging, base)}
    assert second_ids == first_ids


def test_transcript_sync_skips_repos_outside_e3(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    session_logging = load_scripts_module("session_logging")
    load_scripts_module("publish_presence")
    transcript_sync = load_scripts_module("transcript_sync")

    repo = init_git_repo(tmp_path / "repo", "https://github.com/example/other.git")
    transcript = tmp_path / "projects" / "-slug" / "claude-xcript.jsonl"
    transcript.parent.mkdir(parents=True)
    _write_claude_transcript(transcript, repo)

    result = transcript_sync.sync_transcript_records("claude-xcript", transcript)
    assert result["synced"] == 0
    assert not session_logging.pending_queue_paths(tmp_path / "state")


def test_drain_posts_message_and_usage_records_to_shared_ingest(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_SESSION_LOG_INGEST_URL", "https://logs.example.test/ingest")
    session_logging = load_scripts_module("session_logging")
    load_scripts_module("publish_presence")
    transcript_sync = load_scripts_module("transcript_sync")

    repo = init_git_repo(tmp_path / "repo", "https://github.com/e3-solutions/codex-plugins.git")
    transcript = tmp_path / "projects" / "-slug" / "claude-xcript.jsonl"
    transcript.parent.mkdir(parents=True)
    _write_claude_transcript(transcript, repo)
    transcript_sync.sync_transcript_records("claude-xcript", transcript)

    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b""

    monkeypatch.setattr(
        session_logging.urllib.request,
        "urlopen",
        lambda request, timeout: requests.append(request) or Response(),
    )

    result = session_logging.drain_queue()
    assert result["uploaded"] == 4
    bodies = [json.loads(r.data.decode("utf-8")) for r in requests]
    by_type = {}
    for body in bodies:
        by_type.setdefault(body["record"]["type"], []).append(body)

    assert len(by_type["message"]) == 3
    assert len(by_type["usage"]) == 1
    for body in bodies:
        assert body["record"]["metadata"]["agent"] == "claude"
        assert body["client"]["repo_remote"] == "https://github.com/e3-solutions/codex-plugins.git"
    assert all("content" in body["message"] for body in by_type["message"])
    assert by_type["usage"][0]["usage"]["total_tokens"] == 160


def test_auto_update_is_throttled(tmp_path, monkeypatch):
    update_plugin = load_update_plugin()
    state_path = tmp_path / "marketplace-update.json"
    update_plugin.write_state(state_path, {"last_checked_at_epoch": 100.0})
    monkeypatch.setattr(update_plugin.time, "time", lambda: 101.0)
    monkeypatch.setattr(update_plugin.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not run"))

    assert update_plugin.run_update(state_path=state_path) == {"updated": False, "reason": "not_due"}
