"""Sesh is off for automated runs and capped per user turn (COR-4593)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/codex-session-logging/scripts"
sys.path.insert(0, str(SCRIPTS))
import sesh_gate as gate  # noqa: E402

SEARCH = "mcp__e3_cosmos__sesh__search_coding_sessions"
SUGGESTIONS = ("# Overview\n\nGenerate 0 to 3 hyperpersonalized suggestions for what this user "
               "can do with Codex in this local project: /repo")
HEARTBEAT = ("<heartbeat>\n  <automation_id>evaluator-milestone-slack-reminders</automation_id>\n"
             "  <current_time_iso>2026-09-25T11:25:01.864Z</current_time_iso>\n  <instructions>\nReview")
GATE_ENV = ("E3_AUTOMATED_AGENT", "E3_SESH_SEARCH_GATE_ENABLED", "E3_SESH_MAX_SEARCHES_PER_TURN",
            "E3_SESH_CONTEXT_ENABLED", "E3_SESH_START_SEARCH_ENABLED", "FORUM_CUE_ENABLED")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    for name in GATE_ENV:
        monkeypatch.delenv(name, raising=False)


def prompt(text, session="s1", turn="t1", **extra):
    return {"hook_event_name": "UserPromptSubmit", "session_id": session, "turn_id": turn,
            "cwd": "/repo", "prompt": text, **extra}


def search(session="s1", turn="t1", tool=SEARCH, **extra):
    return {"hook_event_name": "PreToolUse", "session_id": session, "turn_id": turn,
            "tool_name": tool, "tool_input": {"query": "x"}, **extra}


@pytest.mark.parametrize("text,reason", [
    (SUGGESTIONS, "suggestion_generator"),
    (HEARTBEAT, "heartbeat_automation"),
    ("You are Linear Progress Sync running inside Codex.\n\nUse the existing Linear", "linear_progress_sync"),
    ("<task>\nRun a stop-gate review of the previous Claude turn.\n", "review_gate"),
    ("<role>\nYou are Codex performing an adversarial software review.\n", "review_gate"),
])
def test_template_prompts_are_recognized(text, reason):
    assert gate.prompt_automation_reason(text) == reason


@pytest.mark.parametrize("text", [
    "Fix the OCR retry bug in the shield worker",
    "Can you generate 0 to 3 suggestions for the deck?",
    "Please explain what <heartbeat> means in Codex automations",  # not at the start
    "Look at the review gate prompt: Run a stop-gate review of the previous Claude turn.",
    None, "",
])
def test_person_prompts_are_not_templates(text):
    assert gate.prompt_automation_reason(text) is None


def test_template_text_late_in_a_long_prompt_is_ignored():
    assert gate.prompt_automation_reason("x" * 500 + "<automation_id>a</automation_id>") is None


@pytest.mark.parametrize("meta,reason", [
    ({"originator": "codex_exec", "source": "cli"}, "codex_exec"),
    ({"originator": "codex_vscode", "source": "exec"}, "codex_exec"),
    ({"originator": "Codex Desktop", "source": {"subagent": {"other": "guardian"}},
      "thread_source": "guardian_review"}, "review_subagent"),
    ({"originator": "Codex Desktop", "source": {"subagent": {"other": "guardian"}}}, "review_subagent"),
    ({"originator": "Codex Desktop", "source": "vscode", "thread_source": "user"}, None),
    ({"originator": "Codex Desktop", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "p"}}},
      "thread_source": "subagent"}, None),
])
def test_rollout_header_signals(tmp_path, meta, reason):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(json.dumps({"type": "session_meta", "payload": meta}) + "\n{}\n")
    assert gate.transcript_automation_reason(str(rollout)) == reason


@pytest.mark.parametrize("path", [None, "", "/does/not/exist.jsonl"])
def test_missing_rollout_is_not_automated(path):
    assert gate.transcript_automation_reason(path) is None


def test_person_turn_allows_two_searches_then_blocks_until_next_turn():
    gate.start_turn(prompt("Why did the evaluator regress?"))
    assert gate.check_sesh_search(search()) is None
    assert gate.check_sesh_search(search()) is None
    blocked = gate.check_sesh_search(search())
    assert blocked and "2 searches per user turn" in blocked
    gate.start_turn(prompt("Search Sesh again for the v1 thresholds", turn="t2"))
    assert gate.check_sesh_search(search(turn="t2")) is None


def test_cap_resets_on_new_turn_id_even_without_prompt_hook():
    for _ in range(2):
        assert gate.check_sesh_search(search(turn="a")) is None
    assert gate.check_sesh_search(search(turn="a"))
    assert gate.check_sesh_search(search(turn="b")) is None


def test_cap_without_turn_ids_uses_prompt_boundaries():
    gate.start_turn({"session_id": "s1", "prompt": "Fix it"}, now=1.0)
    for _ in range(2):
        assert gate.check_sesh_search({"session_id": "s1", "tool_name": SEARCH}) is None
    assert gate.check_sesh_search({"session_id": "s1", "tool_name": SEARCH})
    gate.start_turn({"session_id": "s1", "prompt": "Again"}, now=2.0)
    assert gate.check_sesh_search({"session_id": "s1", "tool_name": SEARCH}) is None


@pytest.mark.parametrize("value,allowed", [("1", 1), ("3", 3), ("0", None), ("off", None), ("junk", 2)])
def test_cap_is_configurable(monkeypatch, value, allowed):
    monkeypatch.setenv("E3_SESH_MAX_SEARCHES_PER_TURN", value)
    gate.start_turn(prompt("Fix it"))
    results = [gate.check_sesh_search(search()) for _ in range(5)]
    expected_allowed = 5 if allowed is None else allowed
    assert results[:expected_allowed] == [None] * expected_allowed
    assert all(results[expected_allowed:])


@pytest.mark.parametrize("text", [SUGGESTIONS, HEARTBEAT])
def test_template_turns_block_every_search(text):
    gate.start_turn(prompt(text))
    blocked = gate.check_sesh_search(search())
    assert blocked and "turned off for automated runs" in blocked


def test_heartbeat_turn_does_not_mark_the_persons_thread():
    gate.start_turn(prompt(HEARTBEAT, turn="hb"))
    assert gate.check_sesh_search(search(turn="hb"))
    gate.start_turn(prompt("Back to the evaluator", turn="person"))
    assert gate.check_sesh_search(search(turn="person")) is None


def test_template_turn_marks_only_that_turn():
    gate.start_turn(prompt(SUGGESTIONS))
    # A template turn marks only that turn; the env and rollout header mark sessions.
    gate.start_turn(prompt("Fix it", turn="t2"))
    assert gate.check_sesh_search(search(turn="t2")) is None


def test_env_marks_whole_session(monkeypatch):
    monkeypatch.setenv("E3_AUTOMATED_AGENT", "1")
    blocked = gate.check_sesh_search(search())
    assert blocked and "(env)" in blocked


def test_gate_off_allows_everything(monkeypatch):
    monkeypatch.setenv("E3_SESH_SEARCH_GATE_ENABLED", "0")
    monkeypatch.setenv("E3_AUTOMATED_AGENT", "1")
    assert all(gate.check_sesh_search(search()) is None for _ in range(5))
    assert gate.max_searches_per_turn() is None


@pytest.mark.parametrize("tool", [
    "mcp__e3__sesh__search_coding_sessions", "mcp__cosmos__sesh__search_coding_sessions",
    "mcp__cosmos_e3__sesh__search_coding_sessions",
])
def test_known_sesh_aliases_are_gated(tool):
    gate.start_turn(prompt(SUGGESTIONS))
    assert gate.check_sesh_search(search(tool=tool))


@pytest.mark.parametrize("tool", ["Bash", "mcp__e3_cosmos__sesh__open_coding_session_source",
                                  "mcp__e3_cosmos__forum__search_research_posts"])
def test_other_tools_are_never_gated_or_counted(tool):
    gate.start_turn(prompt(SUGGESTIONS))
    assert gate.check_sesh_search(search(tool=tool)) is None


def test_state_holds_no_prompt_text(tmp_path):
    gate.start_turn(prompt("secret customer words " + SUGGESTIONS))
    gate.check_sesh_search(search())
    text = "".join(p.read_text() for p in (tmp_path / "codex-home/sesh-gate").rglob("*.json"))
    assert "secret" not in text and "query" not in text


def test_unsafe_session_ids_store_nothing(tmp_path):
    for session in ("../evil", ".hidden", "", None):
        gate.start_turn(prompt("Fix it", session=session))
        assert gate.check_sesh_search(search(session=session)) is None
    assert not (tmp_path / "codex-home/sesh-gate").exists()


def test_corrupt_state_never_breaks_a_call(tmp_path):
    path = tmp_path / "codex-home/sesh-gate/sessions/s1.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert gate.check_sesh_search(search()) is None


# ------------------------------------------------------- hook processes ---

def init_git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/e3-solutions/example.git"],
                   cwd=path, check=True)
    return path


def hook_env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items() if k not in GATE_ENV}
    env.update({
        "CODEX_HOME": str(tmp_path / "codex-home"),
        "CODEX_SESSION_LOG_AUTO_UPLOAD": "0",
        "CODEX_SESSION_LOG_STATE_DIR": str(tmp_path / "codex-state"),
        "E3_COLLECTIVE_FEEDBACK_HOOK_ENABLED": "0",
        "E3_COLLECTIVE_HOOK_ENABLED": "0",
        **extra,
    })
    return env


def run_hook(hook, payload, env):
    return subprocess.run([sys.executable, str(SCRIPTS / f"{hook}.py")], input=json.dumps(payload),
                          text=True, capture_output=True, check=False, env=env, timeout=30)


def additional_context(stdout):
    body = json.loads(stdout)["hookSpecificOutput"]
    assert body["hookEventName"] == "UserPromptSubmit"
    return body["additionalContext"]


def test_hooks_end_to_end_for_a_person(tmp_path):
    repo = init_git_repo(tmp_path / "repo")
    env = hook_env(tmp_path)
    start = run_hook("session_start", {"hook_event_name": "SessionStart", "session_id": "p",
                                       "cwd": str(repo), "source": "startup"}, env)
    assert start.returncode == 0 and "Sesh prior-work context" not in start.stdout
    first = run_hook("user_prompt_submit", prompt("Fix the OCR retry bug", session="p", cwd=str(repo)), env)
    context = additional_context(first.stdout)
    assert context.startswith("Sesh prior-work context")
    assert "Forum check (E3 Collective)" in context  # both cues share one JSON object
    second = run_hook("user_prompt_submit", prompt("and the tests", session="p", turn="t2", cwd=str(repo)), env)
    assert "Sesh prior-work context" not in second.stdout
    codes = [run_hook("pre_tool_use", search(session="p", turn="t2", cwd=str(repo)), env).returncode
             for _ in range(3)]
    assert codes == [0, 0, 2]
    blocked = run_hook("pre_tool_use", search(session="p", turn="t2", cwd=str(repo)), env)
    assert blocked.returncode == 2 and "Sesh search limit reached" in blocked.stderr


def test_hooks_end_to_end_for_the_suggestion_generator(tmp_path):
    repo = init_git_repo(tmp_path / "repo")
    env = hook_env(tmp_path, FORUM_CUE_ENABLED="0")
    run_hook("session_start", {"hook_event_name": "SessionStart", "session_id": "bot",
                               "cwd": str(repo), "source": "startup"}, env)
    submitted = run_hook("user_prompt_submit", prompt(SUGGESTIONS, session="bot", cwd=str(repo)), env)
    assert submitted.returncode == 0 and submitted.stdout.strip() == ""
    blocked = run_hook("pre_tool_use", search(session="bot", cwd=str(repo)), env)
    assert blocked.returncode == 2 and "turned off for automated runs (suggestion_generator)" in blocked.stderr
    other = run_hook("pre_tool_use", search(session="bot", cwd=str(repo), tool="Bash"), env)
    assert other.returncode == 0


def test_hooks_end_to_end_for_an_env_marked_worker(tmp_path):
    repo = init_git_repo(tmp_path / "repo")
    env = hook_env(tmp_path, E3_AUTOMATED_AGENT="1")
    for source in ("startup", "compact"):
        result = run_hook("session_start", {"hook_event_name": "SessionStart", "session_id": "w",
                                            "cwd": str(repo), "source": source}, env)
        assert "Sesh prior-work context" not in result.stdout
    assert run_hook("pre_tool_use", search(session="w", cwd=str(repo)), env).returncode == 2


def test_forum_only_prompt_output_is_unchanged(tmp_path):
    repo = init_git_repo(tmp_path / "repo")
    env = hook_env(tmp_path, E3_SESH_CONTEXT_ENABLED="0")
    sys.path.insert(0, str(SCRIPTS))
    import forum_cue

    run_hook("session_start", {"hook_event_name": "SessionStart", "session_id": "f",
                               "cwd": str(repo), "source": "startup"}, env)
    result = run_hook("user_prompt_submit", prompt("Fix it", session="f", cwd=str(repo)), env)
    assert additional_context(result.stdout) == forum_cue.CUE
