import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "plugins/codex-session-logging/scripts"
sys.path.insert(0, str(SCRIPTS))
import forum_cue as cue  # noqa: E402

E3 = types.SimpleNamespace(returncode=0, stdout="git@github.com:e3-solutions/sesh.git\n")
LONG = " ".join(["word"] * cue.NEW_TASK_MIN_WORDS)


def payload(prompt="fix the timeout", session="s1", **extra):
    return {"hook_event_name": "UserPromptSubmit", "session_id": session, "cwd": "/repo",
            "prompt": prompt, **extra}


@pytest.fixture
def e3(monkeypatch):
    run = Mock(return_value=E3)
    monkeypatch.setattr(cue.subprocess, "run", run)
    return run


def context_of(output):
    body = json.loads(output)["hookSpecificOutput"]
    assert body["hookEventName"] == "UserPromptSubmit"
    return body["additionalContext"]


def test_first_prompt_in_e3_repository_gets_cue_and_log_without_prompt_text(tmp_path, e3):
    output = cue.forum_cue(payload("secret task words"), directory=tmp_path, now=1000.0)
    assert context_of(output) == cue.CUE
    for phrase in (
        "2-4 plain words", "For each post you fully read",
        "before completing the task", "kind=up", "kind=down", "kind=abstain",
        "post_id and body_sha256 from the result", "Concern is optional", "No sentiment is forced",
        "only for an exact retry", "remains unsaved", "name the Forum posts you used",
    ):
        assert phrase in cue.CUE
    # Two calls per useful interaction: no re-read, guide/status call, retry step, or USE/SKIP line.
    for phrase in ("related", "Forum: USE", "get_research_post"):
        assert phrase not in cue.CUE
    log = [json.loads(line) for line in (tmp_path / "cue-log.jsonl").read_text().splitlines()]
    assert log == [{"ts": "1970-01-01T00:16:40Z", "session_id": "s1", "reason": "first_prompt",
                    "prompt_words": 3}]
    assert "secret" not in (tmp_path / "cue-log.jsonl").read_text()
    assert e3.call_args.kwargs["timeout"] == 0.5


def test_same_thread_cues_only_long_new_tasks_after_an_hour(tmp_path, e3):
    assert cue.forum_cue(payload(), directory=tmp_path, now=0.0)
    assert cue.forum_cue(payload("go ahead"), directory=tmp_path, now=10.0) is None
    assert cue.forum_cue(payload(LONG), directory=tmp_path, now=cue.MIN_GAP_SECONDS - 1) is None
    assert cue.forum_cue(payload("progress?"), directory=tmp_path, now=cue.MIN_GAP_SECONDS + 1) is None
    assert cue.forum_cue(payload(LONG), directory=tmp_path, now=cue.MIN_GAP_SECONDS + 1)
    assert cue.forum_cue(payload(session="s2"), directory=tmp_path, now=10.0)


@pytest.mark.parametrize("remote", ["https://github.com/other/sesh", "git@evil:e3-solutions/sesh", ""])
def test_foreign_repository_gets_nothing(tmp_path, monkeypatch, remote):
    monkeypatch.setattr(cue.subprocess, "run", Mock(return_value=types.SimpleNamespace(returncode=0, stdout=remote)))
    assert cue.forum_cue(payload(), directory=tmp_path) is None
    assert not (tmp_path / "cue-log.jsonl").exists()


@pytest.mark.parametrize("bad", [None, {}, payload(hook_event_name="Stop"), payload(session=""),
                                 payload(session="../escape"), payload(cwd=5)])
def test_malformed_or_other_events_spawn_nothing(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(cue.subprocess, "run", Mock(side_effect=AssertionError("must not spawn")))
    assert cue.forum_cue(bad, directory=tmp_path) is None


@pytest.mark.parametrize("value", ["0", "false", "no", "off", " OFF "])
def test_optout(tmp_path, monkeypatch, value):
    monkeypatch.setenv(cue.ENABLED_ENV, value)
    monkeypatch.setattr(cue.subprocess, "run", Mock(side_effect=AssertionError()))
    assert cue.forum_cue(payload(), directory=tmp_path) is None


@pytest.mark.parametrize("failure", [OSError(), cue.subprocess.TimeoutExpired("git", 0.5)])
def test_git_failure(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(cue.subprocess, "run", Mock(side_effect=failure))
    assert cue.forum_cue(payload(), directory=tmp_path) is None


@pytest.mark.parametrize("cue_fails", [False, True])
def test_user_prompt_submit_emits_cue_and_preserves_capture(monkeypatch, capsys, cue_fails):
    calls = []
    monkeypatch.setitem(sys.modules, "rollout_sync", types.SimpleNamespace(
        sync_after_hook=lambda *a, **kw: calls.append("sync")))
    monkeypatch.setitem(sys.modules, "session_logging", types.SimpleNamespace(
        read_stdin_json=lambda: payload(), capture_hook_event=lambda *a, **kw: calls.append("capture")))
    monkeypatch.setattr(cue, "forum_cue", Mock(side_effect=RuntimeError() if cue_fails else None,
                                               return_value='{"hookSpecificOutput": {}}'))
    spec = importlib.util.spec_from_file_location("candidate_prompt", SCRIPTS / "user_prompt_submit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()
    output = capsys.readouterr().out
    assert calls == ["capture", "sync"]
    assert output == ("" if cue_fails else '{"hookSpecificOutput": {}}\n')
