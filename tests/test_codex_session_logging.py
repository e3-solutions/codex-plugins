from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "plugins" / "codex-session-logging" / "scripts" / "session_logging.py"


@pytest.fixture(autouse=True)
def disable_background_uploads_by_default(monkeypatch):
    monkeypatch.setenv("CODEX_SESSION_LOG_AUTO_UPLOAD", "0")


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


def test_session_start_spools_sanitized_environment_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        """
model = "gpt-5.5"
service_tier = "priority"

[plugins."github@openai-curated"]
enabled = true

[plugins."codex-session-logging@coreedge-local"]
enabled = true

[apps.asdk_app_linear.tools."linear.save_issue"]
approval_mode = "approve"

[marketplaces.coreedge-local]
source_type = "local"
source = "/Users/example/codex-plugins"

[mcp_servers.github]
url = "https://api.githubcopilot.com/mcp/"
bearer_token_env_var = "GITHUB_PERSONAL_ACCESS_TOKEN"

[mcp_servers.local-secret]
command = "npx"
args = ["-y", "secret-package", "--token", "sk-do-not-store"]

[mcp_servers.local-secret.env]
SECRET_TOKEN = "sk-do-not-store"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (codex_home / "skills" / "custom-skill").mkdir(parents=True)
    (codex_home / "skills" / "custom-skill" / "SKILL.md").write_text(
        "local skill body with sk-do-not-store",
        encoding="utf-8",
    )
    (codex_home / "skills" / ".system" / "skill-creator").mkdir(parents=True)
    (codex_home / "skills" / ".system" / "skill-creator" / "SKILL.md").write_text(
        "system skill body",
        encoding="utf-8",
    )
    plugin_skill_dir = (
        codex_home
        / "plugins"
        / "cache"
        / "openai-curated"
        / "build-web-apps"
        / "d6169bef"
        / "skills"
        / "frontend-app-builder"
    )
    plugin_skill_dir.mkdir(parents=True)
    (
        plugin_skill_dir / "SKILL.md"
    ).write_text("plugin skill body", encoding="utf-8")
    agents_home = tmp_path / "agents-home"
    (agents_home / "skills" / "find-skills").mkdir(parents=True)
    (agents_home / "skills" / "find-skills" / "SKILL.md").write_text(
        "agent skill body",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("AGENTS_HOME", str(agents_home))
    session_logging = load_session_logging()
    repo = init_git_repo(tmp_path / "repo", "https://github.com/e3-solutions/codex-plugins.git")

    result = session_logging.capture_hook_event(
        {
            "hook_event_name": "SessionStart",
            "session_id": "session-setup",
            "cwd": str(repo),
        }
    )

    detail = json.loads((tmp_path / "state" / result["local_content_path"]).read_text(encoding="utf-8"))
    detail_text = json.dumps(detail, sort_keys=True)

    assert result["type"] == "event"
    assert result["event_type"] == "environment_snapshot"
    assert detail["event_type"] == "environment_snapshot"
    assert detail["metadata"]["codex_setup"]["plugins"] == [
        {"enabled": True, "name": "codex-session-logging@coreedge-local"},
        {"enabled": True, "name": "github@openai-curated"},
    ]
    assert detail["metadata"]["codex_setup"]["mcp_servers"] == [
        {"name": "github", "transport": "url"},
        {"name": "local-secret", "transport": "command"},
    ]
    assert detail["metadata"]["codex_setup"]["connections"] == [
        {"id": "asdk_app_linear", "tools": ["linear.save_issue"]},
    ]
    assert {
        json.dumps(skill, sort_keys=True)
        for skill in detail["metadata"]["codex_setup"]["skills"]
    } == {
        '{"name": "custom-skill", "source": "user"}',
        '{"name": "find-skills", "source": "agent"}',
        '{"marketplace": "openai-curated", "name": "frontend-app-builder", "plugin": "build-web-apps", "source": "plugin", "version": "d6169bef"}',
        '{"name": "skill-creator", "source": "system"}',
    }
    assert detail["metadata"]["codex_setup"]["settings"]["model"] == "gpt-5.5"
    assert "sk-do-not-store" not in detail_text
    assert "SECRET_TOKEN" not in detail_text
    assert "bearer_token_env_var" not in detail_text


def test_pre_tool_use_records_only_tool_name_without_arguments(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    session_logging = load_session_logging()
    repo = init_git_repo(tmp_path / "repo", "https://github.com/e3-solutions/codex-plugins.git")

    result = session_logging.capture_hook_event(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "session-tools",
            "cwd": str(repo),
            "tool_name": "functions.exec_command",
            "tool_input": {"cmd": "echo super-secret-value"},
        }
    )

    detail = json.loads((tmp_path / "state" / result["local_content_path"]).read_text(encoding="utf-8"))
    detail_text = json.dumps(detail, sort_keys=True)

    assert result["type"] == "event"
    assert result["event_type"] == "tool_call_started"
    assert detail["metadata"] == {
        "cwd": str(repo),
        "tool_name": "functions.exec_command",
        "tool_phase": "started",
    }
    assert "tool_input" not in detail_text
    assert "cmd" not in detail_text
    assert "super-secret-value" not in detail_text


def test_post_tool_use_records_tool_completion_without_output(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    session_logging = load_session_logging()
    repo = init_git_repo(tmp_path / "repo", "https://github.com/e3-solutions/codex-plugins.git")

    result = session_logging.capture_hook_event(
        {
            "hook_event_name": "PostToolUse",
            "session_id": "session-tools",
            "cwd": str(repo),
            "tool": {"name": "web.run"},
            "success": True,
            "tool_response": "large output should not be stored",
        }
    )

    detail = json.loads((tmp_path / "state" / result["local_content_path"]).read_text(encoding="utf-8"))
    detail_text = json.dumps(detail, sort_keys=True)

    assert result["type"] == "event"
    assert result["event_type"] == "tool_call_finished"
    assert detail["metadata"] == {
        "cwd": str(repo),
        "success": True,
        "tool_name": "web.run",
        "tool_phase": "finished",
    }
    assert "tool_response" not in detail_text
    assert "large output" not in detail_text


def test_parallel_hook_processes_allocate_unique_sequence_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_SESSION_LOG_AUTO_UPLOAD", "0")
    repo = init_git_repo(tmp_path / "repo", "https://github.com/e3-solutions/codex-plugins.git")
    payloads = [
        {
            "hook_event_name": "PreToolUse",
            "session_id": "parallel-session",
            "cwd": str(repo),
            "tool_name": f"tool-{index}",
        }
        for index in range(80)
    ]
    processes = [
        subprocess.Popen(
            [sys.executable, str(MODULE_PATH), "capture", "--event", "PreToolUse"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={
                **os.environ,
                "CODEX_SESSION_LOG_STATE_DIR": str(tmp_path / "state"),
                "CODEX_SESSION_LOG_AUTO_UPLOAD": "0",
            },
            cwd=ROOT,
        )
        for _ in payloads
    ]

    for process, payload in zip(processes, payloads, strict=True):
        assert process.stdin is not None
        process.stdin.write(json.dumps(payload))
        process.stdin.close()
    results = []
    for process in processes:
        process.wait(timeout=10)
        assert process.stdout is not None
        assert process.stderr is not None
        results.append((process.stdout.read(), process.stderr.read()))

    for process, (_stdout, stderr) in zip(processes, results, strict=True):
        assert process.returncode == 0, stderr
    records = read_queue_records(tmp_path / "state")
    seqs = [record["seq"] for record in records]
    local_paths = [record["local_content_path"] for record in records]

    assert len(records) == len(payloads)
    assert len(set(seqs)) == len(payloads)
    assert len(set(local_paths)) == len(payloads)


def test_capture_spawns_background_drain_without_uploading_inline(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_SESSION_LOG_AUTO_UPLOAD", "1")
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
    monkeypatch.setattr(session_logging.IngestUploader, "from_env", classmethod(lambda cls: uploader))

    result = session_logging.drain_queue()
    queued = read_queue_records(tmp_path / "state")

    assert result["uploaded"] == 2
    assert result["remaining"] == 0
    assert queued == []


def test_drain_posts_full_message_to_ingest_endpoint_without_local_supabase_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_SESSION_LOG_INGEST_URL", "https://logs.example.test/ingest")
    session_logging = load_session_logging()
    repo = init_git_repo(tmp_path / "repo", "https://github.com/e3-solutions/codex-plugins.git")

    session_logging.capture_hook_event(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-123",
            "turn_id": "turn-1",
            "cwd": str(repo),
            "prompt": "Captured before user id is configured.",
        }
    )

    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"ok":true}'

    def fake_urlopen(request, timeout):
        requests.append(request)
        return Response()

    monkeypatch.setattr(session_logging.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(session_logging.socket, "gethostname", lambda: "arya-mbp")
    monkeypatch.setattr(session_logging.getpass, "getuser", lambda: "arya")
    monkeypatch.setattr(session_logging, "git_config_value", lambda cwd, key: None)

    result = session_logging.drain_queue()
    body = json.loads(requests[0].data.decode("utf-8"))

    assert result["uploaded"] == 1
    assert requests[0].full_url == "https://logs.example.test/ingest"
    assert requests[0].headers["Content-type"] == "application/json"
    assert "Authorization" not in requests[0].headers
    assert body["record"]["session_id"] == "session-123"
    assert body["message"]["content"] == "Captured before user id is configured."
    assert body["client"]["repo_remote"] == "https://github.com/e3-solutions/codex-plugins.git"
    assert body["client"]["hostname"] == "arya-mbp"
    assert body["client"]["local_username"] == "arya"
    assert body["client"]["identity_key"] == f"installation:{body['client']['installation_id']}"
    assert len(body["client"]["installation_id"]) > 0
    assert "git_email" not in body["client"]


def test_drain_posts_event_payload_to_ingest_endpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_SESSION_LOG_INGEST_URL", "https://logs.example.test/ingest")
    session_logging = load_session_logging()
    repo = init_git_repo(tmp_path / "repo", "https://github.com/e3-solutions/codex-plugins.git")

    session_logging.capture_hook_event(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "session-tools",
            "cwd": str(repo),
            "tool_name": "functions.exec_command",
            "tool_input": {"cmd": "echo should-not-upload"},
        }
    )

    requests = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"ok":true}'

    monkeypatch.setattr(session_logging.urllib.request, "urlopen", lambda request, timeout: requests.append(request) or Response())

    result = session_logging.drain_queue()
    body = json.loads(requests[0].data.decode("utf-8"))
    body_text = json.dumps(body, sort_keys=True)

    assert result["uploaded"] == 1
    assert body["record"]["type"] == "event"
    assert body["event"]["event_type"] == "tool_call_started"
    assert body["event"]["metadata"]["tool_name"] == "functions.exec_command"
    assert "should-not-upload" not in body_text


def test_drain_dead_letters_permanent_ingest_rejection(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_SESSION_LOG_INGEST_URL", "https://logs.example.test/ingest")
    session_logging = load_session_logging()
    repo = init_git_repo(tmp_path / "repo", "https://github.com/e3-solutions/codex-plugins.git")

    captured = session_logging.capture_hook_event(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-bad-record",
            "cwd": str(repo),
            "prompt": "This record should not retry forever.",
        }
    )

    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"error":"invalid_payload","message":"client.repo_remote must be a non-empty string"}'),
        )

    monkeypatch.setattr(session_logging.urllib.request, "urlopen", fake_urlopen)

    result = session_logging.drain_queue()
    queued = read_queue_records(tmp_path / "state")
    dead_letter_path = (
        tmp_path
        / "state"
        / "queue"
        / "dead-letter"
        / f"{captured['id']}.json"
    )
    dead_lettered = json.loads(dead_letter_path.read_text(encoding="utf-8"))

    assert result == {"uploaded": 0, "failed": 0, "dead_lettered": 1, "remaining": 0}
    assert queued == []
    assert dead_lettered["id"] == captured["id"]
    assert dead_lettered["dead_letter_reason"] == "permanent_upload_failure"
    assert "client.repo_remote" in dead_lettered["last_upload_error"]


def test_drain_dead_letters_record_missing_repo_context_without_network(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_SESSION_LOG_INGEST_URL", "https://logs.example.test/ingest")
    session_logging = load_session_logging()
    repo = init_git_repo(tmp_path / "repo", "https://github.com/e3-solutions/codex-plugins.git")

    captured = session_logging.capture_hook_event(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-stale-record",
            "cwd": str(repo),
            "prompt": "This stale record has no repo metadata.",
        }
    )
    pending_path = tmp_path / "state" / "queue" / "pending" / f"{captured['id']}.json"
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    pending["metadata"] = {}
    pending_path.write_text(json.dumps(pending, sort_keys=True) + "\n", encoding="utf-8")

    def fail_urlopen(request, timeout):
        raise AssertionError("invalid local records should not be posted")

    monkeypatch.setattr(session_logging.urllib.request, "urlopen", fail_urlopen)

    result = session_logging.drain_queue()
    queued = read_queue_records(tmp_path / "state")
    dead_letter_path = tmp_path / "state" / "queue" / "dead-letter" / f"{captured['id']}.json"
    dead_lettered = json.loads(dead_letter_path.read_text(encoding="utf-8"))

    assert result == {"uploaded": 0, "failed": 0, "dead_lettered": 1, "remaining": 0}
    assert queued == []
    assert dead_lettered["dead_letter_reason"] == "permanent_upload_failure"
    assert dead_lettered["last_upload_error"] == "client.repo_remote must be a non-empty string"


def test_drain_keeps_transient_ingest_failure_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_SESSION_LOG_INGEST_URL", "https://logs.example.test/ingest")
    session_logging = load_session_logging()
    repo = init_git_repo(tmp_path / "repo", "https://github.com/e3-solutions/codex-plugins.git")

    captured = session_logging.capture_hook_event(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-transient",
            "cwd": str(repo),
            "prompt": "This record should retry later.",
        }
    )

    def fake_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            500,
            "Internal Server Error",
            {},
            io.BytesIO(b'{"error":"ingest_failed"}'),
        )

    monkeypatch.setattr(session_logging.urllib.request, "urlopen", fake_urlopen)

    result = session_logging.drain_queue()
    queued = read_queue_records(tmp_path / "state")

    assert result == {"uploaded": 0, "failed": 1, "dead_lettered": 0, "remaining": 1}
    assert queued[0]["id"] == captured["id"]
    assert "500" in queued[0]["last_upload_error"]


def test_ingest_payload_includes_git_identity_hints_for_server_side_user_mapping(tmp_path, monkeypatch):
    session_logging = load_session_logging()
    repo = init_git_repo(tmp_path / "repo", "https://github.com/e3-solutions/codex-plugins.git")
    subprocess.run(["git", "config", "user.email", "arya@e3.solutions"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Arya"], cwd=repo, check=True)
    message_path = tmp_path / "users" / "local" / "sessions" / "session-123" / "messages" / "000001-user.json"
    message_path.parent.mkdir(parents=True)
    message_path.write_text('{"content": "map me", "role": "user"}\n', encoding="utf-8")
    record = {
        "id": "22222222222222222222222222222222",
        "session_id": "session-123",
        "turn_id": "turn-1",
        "seq": 1,
        "role": "user",
        "storage_path": "users/local/sessions/session-123/messages/000001-user.json",
        "local_content_path": "users/local/sessions/session-123/messages/000001-user.json",
        "content_sha256": "abc123",
        "content_byte_size": 6,
        "content_excerpt": "map me",
        "metadata": {"cwd": str(repo)},
        "created_at": "2026-07-03T00:00:00+00:00",
    }

    monkeypatch.setattr(session_logging.socket, "gethostname", lambda: "arya-mbp")
    monkeypatch.setattr(session_logging.getpass, "getuser", lambda: "arya")

    payload = session_logging.build_ingest_payload(record, base=tmp_path)

    assert payload["message"]["content"] == "map me"
    assert payload["client"]["git_email"] == "arya@e3.solutions"
    assert payload["client"]["git_user_name"] == "Arya"
    assert payload["client"]["repo_remote"] == "https://github.com/e3-solutions/codex-plugins.git"
    assert payload["client"]["hostname"] == "arya-mbp"


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
    function_path = (
        ROOT
        / "plugins"
        / "codex-session-logging"
        / "supabase"
        / "functions"
        / "codex-session-ingest"
        / "index.ts"
    )
    client_identity_path = function_path.with_name("client_identity.ts")
    config_path = ROOT / "plugins" / "codex-session-logging" / "supabase" / "config.toml"

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
    assert "SessionStart" in hooks["hooks"]
    assert "PreToolUse" in hooks["hooks"]
    assert "PostToolUse" in hooks["hooks"]
    assert "UserPromptSubmit" in hooks["hooks"]
    assert "Stop" in hooks["hooks"]
    assert (ROOT / "plugins" / "codex-session-logging" / "scripts" / "session_start.py").exists()
    assert (ROOT / "plugins" / "codex-session-logging" / "scripts" / "pre_tool_use.py").exists()
    assert (ROOT / "plugins" / "codex-session-logging" / "scripts" / "post_tool_use.py").exists()
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
    assert function_path.exists()
    function_source = function_path.read_text(encoding="utf-8")
    identity_source = client_identity_path.read_text(encoding="utf-8")
    assert "SUPABASE_SECRET_KEYS" in function_source
    assert "CODEX_SESSION_LOG_USER_EMAIL_MAP" in identity_source
    assert "deterministicUserIdForEmail" in identity_source
    assert "clientIdentityKey" in identity_source
    assert "upsertEvent" in function_source
    assert "unknown_user_email" not in function_source
    assert "verify_jwt = false" in config_path.read_text(encoding="utf-8")
