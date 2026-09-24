"""Claude Code copy of the decision-time Forum cue: same text and gating, Claude state location."""
import importlib.util
import inspect
import json
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLAUDE_SCRIPTS = ROOT / "plugins/claude-session-logging/scripts"
CODEX_SCRIPTS = ROOT / "plugins/codex-session-logging/scripts"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cue = load(CLAUDE_SCRIPTS / "forum_cue.py", "claude_forum_cue")
codex_cue = load(CODEX_SCRIPTS / "forum_cue.py", "codex_forum_cue_for_parity")
E3 = types.SimpleNamespace(returncode=0, stdout="https://github.com/e3-solutions/sesh.git\n")
LONG = " ".join(["word"] * cue.NEW_TASK_MIN_WORDS)


def payload(prompt="fix the timeout", session="0b8f6c1e-claude", **extra):
    return {"hook_event_name": "UserPromptSubmit", "session_id": session, "cwd": "/repo",
            "prompt": prompt, "transcript_path": "/t.jsonl", "permission_mode": "default", **extra}


@pytest.fixture
def e3(monkeypatch):
    run = Mock(return_value=E3)
    monkeypatch.setattr(cue.subprocess, "run", run)
    return run


def test_claude_copy_matches_codex_cue_text_and_gating():
    assert cue.CUE == codex_cue.CUE
    for name in ("ENABLED_ENV", "MIN_GAP_SECONDS", "NEW_TASK_MIN_WORDS"):
        assert getattr(cue, name) == getattr(codex_cue, name)
    assert cue._E3_REMOTE.pattern == codex_cue._E3_REMOTE.pattern
    for name in ("forum_cue", "_enabled", "_e3_repository"):
        assert inspect.getsource(getattr(cue, name)) == inspect.getsource(getattr(codex_cue, name))
    assert "search_id" in cue.CUE and cue.ENABLED_ENV == "FORUM_CUE_ENABLED"


def test_state_lives_under_claude_session_logging_state(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_SESSION_LOG_STATE_DIR", raising=False)
    monkeypatch.setattr(cue.Path, "home", lambda: tmp_path)
    assert cue.state_dir() == tmp_path / ".claude" / "session-logging" / "forum-cue"
    monkeypatch.setenv("CLAUDE_SESSION_LOG_STATE_DIR", str(tmp_path / "custom"))
    assert cue.state_dir() == tmp_path / "custom" / "forum-cue"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    assert "codex" not in str(cue.state_dir())


def test_first_prompt_json_shape_and_log_has_no_prompt_text(tmp_path, e3):
    output = cue.forum_cue(payload("secret claude task words"), directory=tmp_path, now=1000.0)
    assert json.loads(output) == {"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": cue.CUE}}
    raw = (tmp_path / "cue-log.jsonl").read_text()
    assert [json.loads(line) for line in raw.splitlines()] == [{
        "ts": "1970-01-01T00:16:40Z", "session_id": "0b8f6c1e-claude",
        "reason": "first_prompt", "prompt_words": 4}]
    assert "secret" not in raw
    marker = tmp_path / "sessions" / "0b8f6c1e-claude"
    assert marker.read_text() == "1000.0"
    assert e3.call_args.args[0][:3] == ["git", "-C", "/repo"]


def test_same_session_cues_only_long_new_tasks_after_an_hour(tmp_path, e3):
    assert cue.forum_cue(payload(), directory=tmp_path, now=0.0)
    assert cue.forum_cue(payload("go ahead"), directory=tmp_path, now=10.0) is None
    assert cue.forum_cue(payload(LONG), directory=tmp_path, now=cue.MIN_GAP_SECONDS - 1) is None
    assert cue.forum_cue(payload("status?"), directory=tmp_path, now=cue.MIN_GAP_SECONDS + 1) is None
    assert cue.forum_cue(payload(LONG), directory=tmp_path, now=cue.MIN_GAP_SECONDS + 1)
    reasons = [json.loads(line)["reason"] for line in (tmp_path / "cue-log.jsonl").read_text().splitlines()]
    assert reasons == ["first_prompt", "new_task"]


@pytest.mark.parametrize("remote", ["https://github.com/other/sesh", "git@evil:e3-solutions/sesh",
                                    "https://github.com/e3-solutions-else/sesh", ""])
def test_non_e3_repository_gets_nothing_and_writes_nothing(tmp_path, monkeypatch, remote):
    monkeypatch.setattr(cue.subprocess, "run", Mock(return_value=types.SimpleNamespace(returncode=0, stdout=remote)))
    assert cue.forum_cue(payload(), directory=tmp_path) is None
    assert not any(tmp_path.iterdir())


@pytest.mark.parametrize("value", ["0", "false", "no", "off", " OFF "])
def test_optout(tmp_path, monkeypatch, value):
    monkeypatch.setenv("FORUM_CUE_ENABLED", value)
    monkeypatch.setattr(cue.subprocess, "run", Mock(side_effect=AssertionError("must not spawn")))
    assert cue.forum_cue(payload(), directory=tmp_path) is None
    assert not any(tmp_path.iterdir())


@pytest.mark.parametrize("cue_fails", [False, True])
def test_claude_user_prompt_submit_prints_cue_first_and_keeps_capture(monkeypatch, capsys, cue_fails):
    order = []

    def fake_cue(_payload):
        order.append("cue")
        if cue_fails:
            raise RuntimeError("boom")
        return '{"hookSpecificOutput": {}}'

    monkeypatch.setitem(sys.modules, "forum_cue", types.SimpleNamespace(forum_cue=fake_cue))
    monkeypatch.setitem(sys.modules, "session_logging", types.SimpleNamespace(
        read_stdin_json=lambda: payload(),
        capture_hook_event=lambda p, **kw: order.append(("capture", kw["event_name"]))))
    module = load(CLAUDE_SCRIPTS / "user_prompt_submit.py", "claude_prompt_candidate")
    module.main()
    captured = capsys.readouterr()
    assert order == ["cue", ("capture", "UserPromptSubmit")]
    assert captured.out == ("" if cue_fails else '{"hookSpecificOutput": {}}\n')


def test_claude_user_prompt_submit_swallows_capture_failure(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "forum_cue", types.SimpleNamespace(forum_cue=lambda p: '{"x": 1}'))

    def broken(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setitem(sys.modules, "session_logging", types.SimpleNamespace(
        read_stdin_json=lambda: payload(), capture_hook_event=broken))
    load(CLAUDE_SCRIPTS / "user_prompt_submit.py", "claude_prompt_capture_fail").main()
    captured = capsys.readouterr()
    assert captured.out == '{"x": 1}\n'
    assert "claude-session-logging capture failed" in captured.err
