import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest
SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/codex-session-logging/scripts"
sys.path.insert(0, str(SCRIPTS))
import sesh_context as context


@pytest.mark.parametrize("source", ["startup", "compact"])
def test_boundary(monkeypatch, source):
    run = Mock(return_value=types.SimpleNamespace(returncode=0, stdout="git@github.com:e3-solutions/sesh.git\n"))
    monkeypatch.setattr(context.subprocess, "run", run)
    assert context.sesh_context({"source": source, "cwd": "/repo"}) == context.CONTEXT
    assert run.call_args.kwargs["timeout"] == 0.5


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
    monkeypatch.setattr(context, "sesh_context", Mock(side_effect=RuntimeError() if guidance_fails else None, return_value="Sesh context"))
    spec = importlib.util.spec_from_file_location("candidate_start", SCRIPTS / "session_start.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()
    output = capsys.readouterr().out
    assert calls == ["capture", "sync"]
    assert ("current Forum" if preview == "1" else "legacy Forum") in output
    assert ("Sesh context" in output) is not guidance_fails
