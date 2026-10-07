import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest
SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/codex-session-logging/scripts"
sys.path.insert(0, str(SCRIPTS))
import sesh_context as context


E3_REMOTE = "git@github.com:e3-solutions/sesh.git\n"


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    for name in ("E3_AUTOMATED_AGENT", "E3_SESH_SEARCH_GATE_ENABLED", "E3_SESH_MAX_SEARCHES_PER_TURN",
                 "E3_SESH_CONTEXT_ENABLED", "E3_SESH_START_SEARCH_ENABLED"):
        monkeypatch.delenv(name, raising=False)


def e3_git(monkeypatch):
    run = Mock(return_value=types.SimpleNamespace(returncode=0, stdout=E3_REMOTE))
    monkeypatch.setattr(context.subprocess, "run", run)
    return run


def first_prompt(session, prompt, turn="turn-1"):
    payload = {"session_id": session, "turn_id": turn, "prompt": prompt, "cwd": "/repo"}
    return context.sesh_prompt_context(payload, context.sesh_gate.start_turn(payload))


def test_compact_boundary_gives_cue(monkeypatch):
    run = e3_git(monkeypatch)
    assert context.sesh_context({"source": "compact", "cwd": "/repo", "session_id": "s1"}) == context.context_text()
    assert run.call_args.kwargs["timeout"] == 0.5


def test_startup_cue_waits_for_first_prompt_then_fires_once(monkeypatch):
    e3_git(monkeypatch)
    assert context.sesh_context({"source": "startup", "cwd": "/repo", "session_id": "s1"}) is None
    assert first_prompt("s1", "Fix the OCR retry bug in the shield worker") == context.context_text()
    assert first_prompt("s1", "and the tests too", turn="turn-2") is None


def test_resumed_thread_gets_no_prompt_cue(monkeypatch):
    e3_git(monkeypatch)
    assert first_prompt("never-started", "Fix the OCR retry bug") is None


def test_cue_text_states_per_turn_cap(monkeypatch):
    assert "at most 2 Sesh searches per user turn" in context.context_text()
    monkeypatch.setenv("E3_SESH_MAX_SEARCHES_PER_TURN", "3")
    assert "at most 3 Sesh searches per user turn" in context.context_text()
    monkeypatch.setenv("E3_SESH_SEARCH_GATE_ENABLED", "0")
    assert "per user turn" not in context.context_text() and "{cap}" not in context.context_text()


@pytest.mark.parametrize("prompt", [
    "# Overview\n\nGenerate 0 to 3 hyperpersonalized suggestions for what this user can do with Codex",
    "<heartbeat>\n  <automation_id>evaluator-milestone-slack-reminders</automation_id>\n  <instructions>Review",
    "You are Linear Progress Sync running inside Codex.\n\nUse the existing Linear MCP",
    "<task>\nRun a stop-gate review of the previous Claude turn.\nOnly review the work",
    "<role>\nYou are Codex performing an adversarial software review.\n",
])
def test_template_runs_never_get_the_cue(monkeypatch, prompt):
    e3_git(monkeypatch)
    context.sesh_context({"source": "startup", "cwd": "/repo", "session_id": "bot"})
    assert first_prompt("bot", prompt) is None
    # The deferred cue is consumed, so a later turn does not pick it up either.
    assert first_prompt("bot", "Fix the OCR retry bug", turn="turn-2") is None


def test_heartbeat_turn_suppresses_compaction_cue_only_for_that_turn(monkeypatch):
    e3_git(monkeypatch)
    first_prompt("thread", "<heartbeat>\n  <automation_id>x</automation_id>", turn="hb")
    assert context.sesh_context({"source": "compact", "cwd": "/repo", "session_id": "thread"}) is None
    first_prompt("thread", "Back to the evaluator: why did v2 regress?", turn="person")
    assert context.sesh_context({"source": "compact", "cwd": "/repo", "session_id": "thread"}) == context.context_text()


def test_automated_env_suppresses_without_git(monkeypatch):
    monkeypatch.setenv("E3_AUTOMATED_AGENT", "1")
    monkeypatch.setattr(context.subprocess, "run", Mock(side_effect=AssertionError("must not spawn")))
    assert context.sesh_context({"source": "compact", "cwd": "/repo", "session_id": "s"}) is None
    assert context.sesh_context({"source": "startup", "cwd": "/repo", "session_id": "s"}) is None


def test_codex_exec_rollout_header_suppresses_startup_cue(monkeypatch, tmp_path):
    e3_git(monkeypatch)
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {"originator": "codex_vscode", "source": "exec"}}) + "\n")
    payload = {"source": "startup", "cwd": "/repo", "session_id": "exec", "transcript_path": str(rollout)}
    assert context.sesh_context(payload) is None
    assert first_prompt("exec", "Fix the OCR retry bug") is None


@pytest.mark.parametrize("payload", [None, {}, {"source": "resume"}, {"source": "clear"}, {"source": "compact", "cwd": 5}, {"source": "startup", "cwd": "/repo", "hook_event_name": "Stop"}])
def test_wrong_boundary_no_process(monkeypatch, payload):
    run = Mock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(context.subprocess, "run", run)
    assert context.sesh_context(payload) is None
    run.assert_not_called()


@pytest.mark.parametrize("remote", ["https://github.com/other/sesh", "https://github.com/e3-solutions/sesh?token=x", "git@evil:e3-solutions/sesh", ""])
def test_foreign_remote(monkeypatch, remote):
    monkeypatch.setattr(context.subprocess, "run", Mock(return_value=types.SimpleNamespace(returncode=0, stdout=remote)))
    assert context.sesh_context({"source": "compact", "cwd": "/repo"}) is None
    assert context.sesh_context({"source": "startup", "cwd": "/repo", "session_id": "s"}) is None
    assert first_prompt("s", "Fix the OCR retry bug") is None


@pytest.mark.parametrize("name", ["E3_SESH_CONTEXT_ENABLED", "E3_SESH_START_SEARCH_ENABLED"])
@pytest.mark.parametrize("value", ["0", "false", "no", "off", " OFF "])
def test_optout(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(context.subprocess, "run", Mock(side_effect=AssertionError()))
    assert context.sesh_context({"source": "compact", "cwd": "/repo"}) is None
    assert context.sesh_context({"source": "startup", "cwd": "/repo", "session_id": "s"}) is None


@pytest.mark.parametrize("failure", [OSError(), context.subprocess.TimeoutExpired("git", 0.5)])
def test_git_failure(monkeypatch, failure):
    monkeypatch.setattr(context.subprocess, "run", Mock(side_effect=failure))
    assert context.sesh_context({"source": "compact", "cwd": "/repo"}) is None


@pytest.mark.parametrize("guidance_fails", [False, True])
@pytest.mark.parametrize("preview", ["0", "1"])
def test_existing_hooks_preserved(monkeypatch, capsys, guidance_fails, preview):
    calls = []
    monkeypatch.setenv("E3_COLLECTIVE_FEEDBACK_HOOK_ENABLED", preview)
    payload = {"source": "compact", "cwd": "/repo"}
    monkeypatch.setitem(sys.modules, "collective", types.SimpleNamespace(session_context=lambda **kw: "legacy Forum"))
    monkeypatch.setitem(sys.modules, "collective_feedback", types.SimpleNamespace(feedback_context=lambda *a, **kw: "current Forum"))
    monkeypatch.setitem(sys.modules, "rollout_sync", types.SimpleNamespace(sync_after_hook=lambda *a, **kw: calls.append("sync")))
    monkeypatch.setitem(sys.modules, "session_logging", types.SimpleNamespace(read_stdin_json=lambda: payload, capture_hook_event=lambda *a, **kw: calls.append("capture"), should_prompt_collective=lambda p: True))
    monkeypatch.setattr(context, "sesh_context", Mock(side_effect=RuntimeError() if guidance_fails else None, return_value="Sesh context"))
    spec = importlib.util.spec_from_file_location("candidate_start", SCRIPTS / "session_start.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()
    output = capsys.readouterr().out
    assert calls == ["capture", "sync"]
    assert ("current Forum" if preview == "1" else "legacy Forum") in output
    assert ("Sesh context" in output) is not guidance_fails
