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
        "2-4 words naming the system, tool or component plus the failure or decision",
        "OCR ensemble evaluation", "release branch ancestry",
        "do not include ticket IDs, file paths, function names, or a post title you already know",
        "internal lessons, not vendor capability facts", "For each post you fully read",
        "before completing the task", "kind=up", "kind=down", "kind=abstain",
        "post_id and body_sha256 from the result", "the search_id from that search",
        "Concern is optional", "No sentiment is forced",
        "only for an exact retry", "remains unsaved", "name the Forum posts you used",
    ):
        assert phrase in cue.CUE
    # Two calls per useful interaction: no re-read, guide/status call, retry step, or USE/SKIP line.
    # Abstract mechanism-only queries mostly returned not-applicable posts, so they are no longer asked for.
    for phrase in ("related", "Forum: USE", "get_research_post", "plain words", "repo names"):
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
    monkeypatch.setenv("E3_SESH_PAST_WORK_CUE", "0")  # COR-4811 cue is on by default; test the Forum cue alone
    calls = []
    monkeypatch.setitem(sys.modules, "rollout_sync", types.SimpleNamespace(
        sync_after_hook=lambda *a, **kw: calls.append("sync")))
    monkeypatch.setitem(sys.modules, "session_logging", types.SimpleNamespace(
        read_stdin_json=lambda: payload(), capture_hook_event=lambda *a, **kw: calls.append("capture")))
    monkeypatch.setattr(cue, "forum_cue_decision", Mock(side_effect=RuntimeError() if cue_fails else None,
                                                        return_value=('{"hookSpecificOutput": {}}', None)))
    spec = importlib.util.spec_from_file_location("candidate_prompt", SCRIPTS / "user_prompt_submit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()
    output = capsys.readouterr().out
    assert calls == ["capture", "sync"]
    assert output == ("" if cue_fails else '{"hookSpecificOutput": {}}\n')


# COR-4883 A/B: off unless FORUM_CUE_AB is on; arm is a pure function of the session id.

def arm_of(session):
    import hashlib
    h = hashlib.sha256(f"forum_cue_v2:{session.strip().lower()}".encode()).digest()
    return "v1" if not h[0] & 1 else ("v2_midtask" if h[1] & 1 else "v2")


def sessions_in(arm, count=1):
    found = [f"sess-{i}" for i in range(400) if arm_of(f"sess-{i}") == arm]
    return found[:count]


def test_ab_is_off_by_default_so_every_session_gets_v1_and_no_new_metadata(tmp_path, e3, monkeypatch):
    monkeypatch.delenv(cue.AB_ENV, raising=False)
    for session in sessions_in("v2", 3) + sessions_in("v2_midtask", 3):
        output, meta = cue.forum_cue_decision(payload(session=session), directory=tmp_path, now=5.0)
        assert context_of(output) == cue.CUE and meta is None
    for line in (tmp_path / "cue-log.jsonl").read_text().splitlines():
        assert set(json.loads(line)) == {"ts", "session_id", "reason", "prompt_words"}


@pytest.mark.parametrize("value", ["0", "", "false", "nope"])
def test_ab_needs_an_explicit_on_value(monkeypatch, value):
    monkeypatch.setenv(cue.AB_ENV, value)
    assert not cue.ab_enabled()


@pytest.mark.parametrize("arm", ["v1", "v2", "v2_midtask"])
def test_ab_on_gives_each_arm_its_cue_and_logs_the_arm(tmp_path, e3, monkeypatch, arm):
    monkeypatch.setenv(cue.AB_ENV, "1")
    session = sessions_in(arm)[0]
    output, meta = cue.forum_cue_decision(payload(session=session), directory=tmp_path, now=5.0)
    assert context_of(output) == cue.CUES[arm]
    assert meta == {"forum_cue_experiment": "forum_cue_v2", "forum_cue_arm": arm,
                    "forum_cue_reason": "first_prompt"}
    log = json.loads((tmp_path / "cue-log.jsonl").read_text())
    assert log["arm"] == arm and log["experiment"] == "forum_cue_v2"
    # Gating is unchanged: a short follow-up in the same session gets nothing.
    assert cue.forum_cue_decision(payload("ok", session=session), directory=tmp_path, now=6.0) == (None, None)


def test_arm_is_stable_case_and_space_insensitive_and_roughly_half_control():
    assert cue.cue_arm(" ABC-123 ") == cue.cue_arm("abc-123") == arm_of("abc-123")
    arms = [cue.cue_arm(f"{i:08x}-0000-4000-8000-000000000000") for i in range(4000)]
    assert 0.46 < arms.count("v1") / 4000 < 0.54
    assert 0.21 < arms.count("v2") / 4000 < 0.29 and 0.21 < arms.count("v2_midtask") / 4000 < 0.29


def test_v2_texts_lead_with_rating_and_dup_check_rules_and_name_the_cosmos_tool():
    for text in (cue.CUE_V2, cue.CUE_V2_MIDTASK):
        for phrase in ("forum__search_research_posts on the Cosmos MCP server",
                       "no separate `forum` server", "Rate only posts you used or checked",
                       "leave the rest unrated", "one parallel batch of give_collective_feedback",
                       "search_id", "Off-topic for this query:", "at most 2 searches",
                       "share_with_collective", "remains unsaved", "name the Forum posts you used"):
            assert phrase in text
        # Down keeps Forum's meaning: the latest vote per user counts, so an off-topic down
        # would cancel that user's earlier up on a good post.
        assert "kind=down if its evidence is incorrect or misleading" in text
        assert "For each post you fully read" not in text
    assert "before you commit to an approach" in cue.CUE_V2 and "Do not search yet" not in cue.CUE_V2
    assert "Do not search yet" in cue.CUE_V2_MIDTASK and "Search again when" in cue.CUE_V2_MIDTASK


def test_hook_passes_the_arm_to_the_session_log(monkeypatch, capsys):
    monkeypatch.setenv("E3_SESH_PAST_WORK_CUE", "0")
    seen = []
    meta = {"forum_cue_experiment": "forum_cue_v2", "forum_cue_arm": "v2_midtask",
            "forum_cue_reason": "first_prompt"}
    monkeypatch.setitem(sys.modules, "rollout_sync", types.SimpleNamespace(sync_after_hook=lambda *a, **kw: None))
    monkeypatch.setitem(sys.modules, "session_logging", types.SimpleNamespace(
        read_stdin_json=lambda: payload(), capture_hook_event=lambda *a, **kw: seen.append(kw)))
    monkeypatch.setattr(cue, "forum_cue_decision", Mock(return_value=('{"hookSpecificOutput": {}}', meta)))
    spec = importlib.util.spec_from_file_location("candidate_prompt_ab", SCRIPTS / "user_prompt_submit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.main()
    assert seen == [{"event_name": "UserPromptSubmit", "extra_metadata": meta}]
