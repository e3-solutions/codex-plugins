"""COR-4786: PreToolUse hooks pass the caller's own session id as exclude_session_id to Sesh."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = {
    "claude": ROOT / "plugins" / "claude-session-logging" / "scripts",
    "codex": ROOT / "plugins" / "codex-session-logging" / "scripts",
}
SESH_TOOLS = [
    "mcp__e3_cosmos__sesh__search_coding_sessions",
    "mcp__codex_apps__cosmos__sesh__search_coding_sessions",
    "mcp__e3_mcp__sesh__search_coding_sessions",
    "mcp__cosmos__search_coding_sessions",
    "search_coding_sessions",
    "mcp__e3_cosmos__sesh__ask_sesh",
    "mcp__cosmos__ask_sesh",
    "ask_sesh",
]
NOT_SESH_TOOLS = [
    "Bash",
    "mcp__e3_cosmos__sesh__open_coding_session_source",
    "mcp__e3_cosmos__sesh__find_similar_sessions",
    "mcp__e3_cosmos__sesh__search_coding_sessions_extra",
    "mcp__linear__list_issues",
]


@pytest.fixture(params=sorted(PLUGINS))
def agent(request):
    return request.param


def run_hook(agent, payload, tmp_path, env_extra=None, raw=None):
    env = {
        **os.environ,
        "CLAUDE_SESSION_LOG_STATE_DIR": str(tmp_path / "state"),
        "CODEX_SESSION_LOG_STATE_DIR": str(tmp_path / "state"),
        "CLAUDE_SESSION_LOG_AUTO_UPLOAD": "0",
        "CODEX_SESSION_LOG_AUTO_UPLOAD": "0",
        "HOME": str(tmp_path / "home"),
    }
    env.pop("E3_SESH_EXCLUDE_SELF", None)
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(PLUGINS[agent] / "pre_tool_use.py")],
        input=raw if raw is not None else json.dumps(payload),
        text=True, capture_output=True, check=False, env=env, cwd=str(tmp_path),
    )


def payload(tool="mcp__e3_cosmos__sesh__search_coding_sessions", **overrides):
    base = {
        "hook_event_name": "PreToolUse",
        "session_id": "sess-123",
        "cwd": "/nonexistent",
        "tool_name": tool,
        "tool_input": {"query": "q", "limit": 5},
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize("tool", SESH_TOOLS)
def test_injects_session_id_and_preserves_input(agent, tmp_path, tool):
    original = {"query": "q", "limit": 5, "nested": {"a": [1, 2]}}
    result = run_hook(agent, payload(tool, tool_input=original), tmp_path)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {**original, "exclude_session_id": "sess-123"},
        }
    }


def test_replaces_explicit_null_exclude(agent, tmp_path):
    result = run_hook(agent, payload(tool_input={"query": "q", "exclude_session_id": None}), tmp_path)
    assert json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"] == {
        "query": "q", "exclude_session_id": "sess-123",
    }


@pytest.mark.parametrize(
    "tool_input",
    [
        {"query": "q", "exclude_session_id": "other-sess"},
        {"query": "q", "include_own_session": True},
        {"query": "q", "include_own_session": False},
    ],
)
def test_never_overrides_explicit_choice(agent, tmp_path, tool_input):
    result = run_hook(agent, payload(tool_input=tool_input), tmp_path)
    assert result.returncode == 0
    assert result.stdout == ""


@pytest.mark.parametrize("tool", NOT_SESH_TOOLS)
def test_other_tools_are_untouched(agent, tmp_path, tool):
    result = run_hook(agent, payload(tool), tmp_path)
    assert result.returncode == 0
    assert result.stdout == ""


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "OFF", " False "])
def test_env_opt_out(agent, tmp_path, value):
    result = run_hook(agent, payload(), tmp_path, env_extra={"E3_SESH_EXCLUDE_SELF": value})
    assert result.returncode == 0
    assert result.stdout == ""


@pytest.mark.parametrize("value", ["1", "true", "on", ""])
def test_env_enabled_values_still_inject(agent, tmp_path, value):
    result = run_hook(agent, payload(), tmp_path, env_extra={"E3_SESH_EXCLUDE_SELF": value})
    assert "exclude_session_id" in result.stdout


@pytest.mark.parametrize(
    "session_id",
    [None, "", "   ", 123, ["sess"], "x" * 257, "bad\nid", "bad\x00id", "bad\x1bid", "bad\x7fid"],
)
def test_bad_session_ids_emit_nothing(agent, tmp_path, session_id):
    result = run_hook(agent, payload(session_id=session_id), tmp_path)
    assert result.returncode == 0
    assert result.stdout == ""


def test_session_id_at_length_limit_is_accepted(agent, tmp_path):
    result = run_hook(agent, payload(session_id="x" * 256), tmp_path)
    assert json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]["exclude_session_id"] == "x" * 256


def test_missing_session_id_emits_nothing(agent, tmp_path):
    data = payload()
    del data["session_id"]
    assert run_hook(agent, data, tmp_path).stdout == ""


@pytest.mark.parametrize("tool_input", [None, "query", ["q"], 5])
def test_non_dict_tool_input_emits_nothing(agent, tmp_path, tool_input):
    result = run_hook(agent, payload(tool_input=tool_input), tmp_path)
    assert result.returncode == 0
    assert result.stdout == ""


def test_missing_tool_input_emits_nothing(agent, tmp_path):
    data = payload()
    del data["tool_input"]
    assert run_hook(agent, data, tmp_path).stdout == ""


@pytest.mark.parametrize("raw", ["", "not json", "[1, 2]", "null", '"text"'])
def test_garbage_stdin_prints_nothing_and_exits_zero(agent, tmp_path, raw):
    result = run_hook(agent, None, tmp_path, raw=raw)
    assert result.returncode == 0
    assert result.stdout == ""


def test_capture_failure_does_not_block_injection_or_print_to_stdout(agent, tmp_path, monkeypatch):
    # Importing the real module and breaking capture: stdout must still be exactly the JSON.
    monkeypatch.syspath_prepend(str(PLUGINS[agent]))
    for name in ("session_logging", "pre_tool_use"):
        sys.modules.pop(name, None)
    import io
    import pre_tool_use

    def boom(*_args, **_kwargs):
        raise RuntimeError("capture broke")

    monkeypatch.setattr(pre_tool_use, "capture_hook_event", boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload())))
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    pre_tool_use.main()
    assert json.loads(out.getvalue())["hookSpecificOutput"]["updatedInput"]["exclude_session_id"] == "sess-123"
    assert "capture broke" in err.getvalue()
    for name in ("session_logging", "pre_tool_use"):
        sys.modules.pop(name, None)


def test_helper_exception_prints_nothing(agent, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(PLUGINS[agent]))
    for name in ("session_logging", "pre_tool_use"):
        sys.modules.pop(name, None)
    import io
    import pre_tool_use

    def boom(_payload):
        raise RuntimeError("helper broke")

    monkeypatch.setattr(pre_tool_use, "capture_hook_event", lambda *a, **k: None)
    monkeypatch.setattr(pre_tool_use, "sesh_exclude_hook_output", boom)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload())))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    pre_tool_use.main()
    assert out.getvalue() == ""
    for name in ("session_logging", "pre_tool_use"):
        sys.modules.pop(name, None)


def test_input_is_not_mutated_and_no_query_text_logged(agent, tmp_path):
    secret_query = "fake-query-text-do-not-store"
    result = run_hook(agent, payload(tool_input={"query": secret_query}), tmp_path)
    assert json.loads(result.stdout)["hookSpecificOutput"]["updatedInput"]["query"] == secret_query
    state = tmp_path / "state"
    for path in state.rglob("*") if state.exists() else []:
        if path.is_file():
            assert secret_query not in path.read_text(encoding="utf-8", errors="ignore")
