import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest
SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/codex-session-logging/scripts"
sys.path.insert(0, str(SCRIPTS))
import sesh_context as context


def test_boundary(monkeypatch):
    run = Mock(return_value=types.SimpleNamespace(returncode=0, stdout="git@github.com:e3-solutions/sesh.git\n"))
    monkeypatch.setattr(context.subprocess, "run", run)
    assert context.sesh_context({"source": "startup", "cwd": "/repo"}) == context.CONTEXT
    assert run.call_args.kwargs["timeout"] == 0.5


# COR-4688: no cue after compaction by default, for every thread (bots included).
@pytest.mark.parametrize("value", [None, "", "0", "false", "off", "no", "yes", "enabled"])
def test_no_cue_after_compaction_by_default(monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("E3_SESH_CUE_AFTER_COMPACTION", value)
    run = Mock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(context.subprocess, "run", run)
    assert context.sesh_context({"source": "compact", "cwd": "/repo"}) is None
    run.assert_not_called()


@pytest.mark.parametrize("value", ["1", "true", "on", " ON "])
def test_compaction_cue_can_be_turned_back_on(monkeypatch, value):
    monkeypatch.setenv("E3_SESH_CUE_AFTER_COMPACTION", value)
    monkeypatch.setattr(context.subprocess, "run", Mock(return_value=types.SimpleNamespace(
        returncode=0, stdout="git@github.com:e3-solutions/sesh.git\n")))
    assert context.sesh_context({"source": "compact", "cwd": "/repo"}) == context.COMPACTION_CONTEXT
    # The startup cue is the same either way.
    assert context.sesh_context({"source": "startup", "cwd": "/repo"}) == context.CONTEXT


def test_compaction_cue_still_respects_scope_and_kill_switch(monkeypatch):
    monkeypatch.setenv("E3_SESH_CUE_AFTER_COMPACTION", "1")
    monkeypatch.setattr(context.subprocess, "run", Mock(return_value=types.SimpleNamespace(
        returncode=0, stdout="https://github.com/other/sesh\n")))
    assert context.sesh_context({"source": "compact", "cwd": "/repo"}) is None
    monkeypatch.setenv("E3_SESH_CONTEXT_ENABLED", "0")
    monkeypatch.setattr(context.subprocess, "run", Mock(side_effect=AssertionError()))
    assert context.sesh_context({"source": "compact", "cwd": "/repo"}) is None


def test_startup_cue_asks_for_one_task_search_then_top_result_before_first_edit():
    text = context.CONTEXT
    assert text.startswith("Sesh prior-work context:")
    assert "user's actual task" in text and "one Sesh" in text
    assert "Before your first edit, open the top result" in text
    assert "exact source arguments" in text
    assert "after compaction" not in text
    assert len(text) < len(context.COMPACTION_CONTEXT)
    # Safety lines kept from the previous cue.
    for kept in ("explicit opt-out", "Never send Slack", "do not block work", "search-coding-sessions"):
        assert kept in text


@pytest.mark.parametrize("payload", [None, {}, {"source": "resume"}, {"source": "clear"}, {"source": "compact", "cwd": 5}, {"source": "startup", "cwd": "/repo", "hook_event_name": "Stop"}])
def test_wrong_boundary_no_process(monkeypatch, payload):
    run = Mock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr(context.subprocess, "run", run)
    assert context.sesh_context(payload) is None
    run.assert_not_called()


@pytest.mark.parametrize("remote", ["https://github.com/other/sesh", "https://github.com/e3-solutions/sesh?token=x", "git@evil:e3-solutions/sesh", ""])
def test_foreign_remote(monkeypatch, remote):
    monkeypatch.setattr(context.subprocess, "run", Mock(return_value=types.SimpleNamespace(returncode=0, stdout=remote)))
    assert context.sesh_context({"source": "startup", "cwd": "/repo"}) is None


@pytest.mark.parametrize("name", ["E3_SESH_CONTEXT_ENABLED", "E3_SESH_START_SEARCH_ENABLED"])
@pytest.mark.parametrize("value", ["0", "false", "no", "off", " OFF "])
def test_optout(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    monkeypatch.setattr(context.subprocess, "run", Mock(side_effect=AssertionError()))
    assert context.sesh_context({"source": "startup", "cwd": "/repo"}) is None


@pytest.mark.parametrize("failure", [OSError(), context.subprocess.TimeoutExpired("git", 0.5)])
def test_git_failure(monkeypatch, failure):
    monkeypatch.setattr(context.subprocess, "run", Mock(side_effect=failure))
    assert context.sesh_context({"source": "startup", "cwd": "/repo"}) is None


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
    monkeypatch.setattr(context, "sesh_cue_decision", Mock(side_effect=RuntimeError() if guidance_fails else None, return_value=("Sesh context", None)))
    spec = importlib.util.spec_from_file_location("candidate_start", SCRIPTS / "session_start.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()
    output = capsys.readouterr().out
    assert calls == ["capture", "sync"]
    assert ("current Forum" if preview == "1" else "legacy Forum") in output
    assert ("Sesh context" in output) is not guidance_fails
