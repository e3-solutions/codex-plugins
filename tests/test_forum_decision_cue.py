"""COR-4911: the decision-point Forum cue in codex-session-logging and claude-session-logging."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CODEX = ROOT / "plugins/codex-session-logging/scripts"
CLAUDE = ROOT / "plugins/claude-session-logging/scripts"


def load(folder: Path, alias: str):
    """Import that plugin's forum_decision_cue with its own forum_cue as the sibling import."""
    saved = {key: sys.modules.pop(key) for key in ("forum_cue", "forum_decision_cue") if key in sys.modules}
    sys.path.insert(0, str(folder))
    try:
        spec = importlib.util.spec_from_file_location(alias, folder / "forum_decision_cue.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        import forum_cue  # noqa: F401 - resolved now, from this folder
        module._forum_cue = sys.modules.pop("forum_cue")
        return module
    finally:
        sys.path.remove(str(folder))
        sys.modules.pop("forum_decision_cue", None)
        sys.modules.update(saved)


cue = load(CODEX, "codex_forum_decision_cue")


def session_in(held: bool, start: int = 0) -> str:
    for i in range(start, start + 400):
        session = f"sess-{i}"
        if bool(hashlib.sha256(f"forum_decision_cue:{session}".encode()).digest()[0] & 1) == held:
            return session
    raise AssertionError("no session found")


SHOWN, HELD = session_in(False), session_in(True)


def call(tool="Bash", session=SHOWN, cwd="/repo", **extra):
    return {"hook_event_name": "PreToolUse", "session_id": session, "cwd": cwd, "tool_name": tool,
            "tool_input": {}, **extra}


@pytest.fixture
def e3(monkeypatch):
    """Every cwd is an E3 repository; count the git checks."""
    checks = []
    monkeypatch.setitem(sys.modules, "forum_cue", type(sys)("forum_cue"))
    sys.modules["forum_cue"]._e3_repository = lambda cwd: checks.append(cwd) or True
    return checks


@pytest.fixture(autouse=True)
def _scripts_on_path(monkeypatch):
    monkeypatch.syspath_prepend(str(CODEX))
    # COR-5034: off by default while Forum is hidden; most tests cover it switched back on.
    monkeypatch.setenv(cue.ENV, "1")


def run(n, directory, **kw):
    return [cue.decision_cue(call(**kw), directory=directory) for _ in range(n)]


def test_fires_once_at_the_sixth_tool_call_and_never_again_in_the_turn(tmp_path, e3):
    results = run(9, tmp_path)
    fired = [i for i, (out, event) in enumerate(results, 1) if event]
    assert fired == [6]
    output, event = results[5]
    assert json.loads(output) == {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                         "additionalContext": cue.CUE}}
    assert event == {"event_type": "forum_decision_cue_shown",
                     "metadata": {"forum_decision_cue": "shown", "forum_decision_cue_reason": "call_6",
                                  "forum_decision_cue_index": 1, "forum_searches_in_turn_before": 0,
                                  "forum_decision_cue_version": cue.VERSION}}
    assert len(e3) == 1  # the E3 check runs once per session, not per tool call


@pytest.mark.parametrize("tool", sorted(cue.EDIT_TOOLS))
def test_fires_at_the_first_edit_before_the_sixth_call(tmp_path, e3, tool):
    run(2, tmp_path)
    output, event = cue.decision_cue(call(tool=tool), directory=tmp_path)
    assert output and event["metadata"]["forum_decision_cue_reason"] == "first_edit"
    assert cue.decision_cue(call(tool=tool), directory=tmp_path) == (None, None)


def test_forum_tools_count_searches_but_never_trigger(tmp_path, e3):
    run(4, tmp_path)
    tool = "mcp__cosmos__forum__search_research_posts"
    assert cue.decision_cue(call(tool=tool), directory=tmp_path) == (None, None)
    assert cue.decision_cue(call(tool="mcp__cosmos__forum__give_collective_feedback"),
                            directory=tmp_path) == (None, None)
    _, event = cue.decision_cue(call(), directory=tmp_path)
    assert event["metadata"]["forum_searches_in_turn_before"] == 1


def test_a_new_turn_resets_and_a_session_gets_at_most_three(tmp_path, e3):
    fires = 0
    for _ in range(5):
        fires += sum(1 for _, event in run(7, tmp_path) if event)
        cue.new_turn({"session_id": SHOWN}, directory=tmp_path)
    assert fires == cue.MAX_PER_SESSION


def test_new_turn_writes_nothing_for_a_session_never_seen(tmp_path):
    cue.new_turn({"session_id": "never-seen"}, directory=tmp_path)
    assert not (tmp_path / "decision").exists()


def test_holdout_logs_withheld_and_shows_nothing(tmp_path, e3):
    output, event = run(6, tmp_path, session=HELD)[-1]
    assert output is None
    assert event["event_type"] == "forum_decision_cue_withheld"
    assert event["metadata"]["forum_decision_cue"] == "withheld"


def test_holdout_is_about_half_of_sessions():
    held = sum(cue.held_out(f"session-{i}") for i in range(2000))
    assert 900 < held < 1100


def test_not_an_e3_repository_never_fires(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "forum_cue", type(sys)("forum_cue"))
    sys.modules["forum_cue"]._e3_repository = lambda cwd: False
    assert all(result == (None, None) for result in run(8, tmp_path, tool="apply_patch"))


def test_off_by_default_while_forum_is_hidden(tmp_path, e3, monkeypatch):
    # COR-5034: unset means no cue, no exposure event, no git check and no state.
    monkeypatch.delenv(cue.ENV, raising=False)
    assert cue.DEFAULT == "0" and not cue.enabled() and not cue.enabled({})
    assert all(result == (None, None) for result in run(8, tmp_path, tool="apply_patch"))
    cue.new_turn({"session_id": SHOWN}, directory=tmp_path)
    assert e3 == [] and not any(tmp_path.iterdir())


@pytest.mark.parametrize("value", ["1", "true", "on", "yes"])
def test_setting_the_flag_on_restores_the_cue(tmp_path, e3, monkeypatch, value):
    monkeypatch.setenv(cue.ENV, value)
    output, event = cue.decision_cue(call(tool="apply_patch"), directory=tmp_path)
    assert json.loads(output)["hookSpecificOutput"]["additionalContext"] == cue.CUE
    assert event["event_type"] == "forum_decision_cue_shown"


@pytest.mark.parametrize("value", ["0", "false", "no", "off"])
def test_switch_off(tmp_path, e3, monkeypatch, value):
    monkeypatch.setenv(cue.ENV, value)
    assert all(result == (None, None) for result in run(8, tmp_path, tool="apply_patch"))


@pytest.mark.parametrize("bad", [{}, {"session_id": "../x", "cwd": "/r", "tool_name": "Bash"},
                                 {"session_id": "s", "cwd": "/r"},
                                 {"hook_event_name": "PostToolUse", "session_id": "s", "cwd": "/r",
                                  "tool_name": "Bash"}])
def test_malformed_or_other_events_do_nothing(tmp_path, e3, bad):
    assert cue.decision_cue(bad, directory=tmp_path) == (None, None)


def test_merge_outputs_joins_two_contexts():
    a = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "A"}})
    b = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "B"}})
    assert json.loads(cue.merge_outputs(a, b))["hookSpecificOutput"]["additionalContext"] == "A\n\nB"
    assert cue.merge_outputs(None, b) == b and cue.merge_outputs(a, None) == a


def test_codex_and_claude_copies_are_identical():
    assert (CODEX / "forum_decision_cue.py").read_bytes() == (CLAUDE / "forum_decision_cue.py").read_bytes()


def test_state_location_follows_each_plugins_forum_cue(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CLAUDE_SESSION_LOG_STATE_DIR", str(tmp_path / "claude"))
    codex, claude = load(CODEX, "codex_dc_paths"), load(CLAUDE, "claude_dc_paths")
    assert codex._forum_cue.state_dir() == tmp_path / "codex" / "forum-cue"
    assert claude._forum_cue.state_dir() == tmp_path / "claude" / "forum-cue"


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin",
                    "https://github.com/e3-solutions/forum.git"], check=True)
    return path


@pytest.mark.parametrize("plugin", ["codex", "claude"])
@pytest.mark.parametrize("flag", ["1", None])
def test_hook_scripts_print_the_cue_on_the_sixth_call_end_to_end(tmp_path, plugin, flag):
    """Runs the real pre_tool_use.py and user_prompt_submit.py in a temporary home, upload off.

    COR-5034: with FORUM_DECISION_CUE unset the hooks print nothing; =1 brings back the cue.
    """
    repo = _git_repo(tmp_path / "repo")
    scripts = CODEX if plugin == "codex" else CLAUDE
    env = {"PATH": "/usr/bin:/bin:/opt/homebrew/bin", "HOME": str(tmp_path / "home"),
           "CODEX_HOME": str(tmp_path / "home/.codex"),
           "CLAUDE_SESSION_LOG_STATE_DIR": str(tmp_path / "home/claude-state"),
           "CODEX_SESSION_LOG_STATE_DIR": str(tmp_path / "home/codex-state"),
           "CODEX_SESSION_LOG_AUTO_UPLOAD": "0", "CLAUDE_SESSION_LOG_AUTO_UPLOAD": "0",
           "E3_SESH_PLAN_CUE": "0", "E3_SESH_ONE_PER_TURN": "0", "FORUM_CUE_ENABLED": "0"}
    if flag is not None:
        env["FORUM_DECISION_CUE"] = flag
    (tmp_path / "home").mkdir()

    def hook(name, payload):
        result = subprocess.run([sys.executable, str(scripts / name)], input=json.dumps(payload),
                                capture_output=True, text=True, env=env, cwd=repo, timeout=60)
        return result.stdout.strip()

    session = SHOWN
    hook("user_prompt_submit.py", {"hook_event_name": "UserPromptSubmit", "session_id": session,
                                   "cwd": str(repo), "prompt": "fix the timeout",
                                   "transcript_path": str(tmp_path / "t.jsonl")})
    outputs = [hook("pre_tool_use.py", {**call(session=session, cwd=str(repo)),
                                        "transcript_path": str(tmp_path / "t.jsonl"),
                                        "tool_use_id": f"t{i}", "turn_id": "turn-1"})
               for i in range(7)]
    if flag is None:
        assert outputs == [""] * 7
        assert not list((tmp_path / "home").rglob("decision"))
        return
    assert [bool(o) for o in outputs] == [False] * 5 + [True, False]
    assert json.loads(outputs[5])["hookSpecificOutput"]["additionalContext"] == cue.CUE
