"""COR-4811: Sesh fires on need. E3_SESH_ONE_PER_TURN and E3_SESH_PAST_WORK_CUE, both off by default."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import threading
import types
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/codex-session-logging/scripts"
sys.path.insert(0, str(SCRIPTS))
import sesh_context  # noqa: E402
import sesh_need as need  # noqa: E402

BASE_COMMIT = "e0ba97d"  # origin/main before COR-4811
SESH_SEARCH = "mcp__e3_cosmos__sesh__search_coding_sessions"
GUARD_ON = {"E3_SESH_ONE_PER_TURN": "1"}
CUE_ON = {"E3_SESH_PAST_WORK_CUE": "1"}
OFF_VALUES = [None, "", "0", "false", "no", "off", "yes", "enabled"]

# Paraphrased from the Oct 16, 2026 study's labels (outputs/sesh-when-to-fire/signal_check.json):
# a person asking about earlier work that past sessions held.
POSITIVE = [
    "I remember adding a migration step for this last week, find it and reuse it",
    "We used to have team alerts in the old channel before the move, how was that set up?",
    "Which change hardcoded the routing flag to true?",
    "What testing did we run on the voice agent before the GPU switch?",
    "Look at Alex's sessions from yesterday and tell me what infra changed",
    "Go through the threads on my machine and review what I asked about the demo",
    "Summarize the changes we've made to the demo this week",
    "Explain what we did and why for the VIN rotation",
    "We previously pinned the orientation classifier, which version was it?",
    "How did heartbeat metrics work before we switched collectors?",
    "When we were sending SMS through the old provider, what limit did we hit?",
    "Pick up from codex://threads/019a0c52 and continue",
]
# Past-tense wording that is not a person asking about earlier work: same-chat references,
# monitoring loops, relays, receipts, pasted docs, and ordinary debugging or build asks.
NEGATIVE = [
    "Keep polling and report any previously terminal calls that changed status",
    "Correction to my previous message: that hash is stale",
    "As previously agreed, go ahead with option B",
    "Like we discussed earlier in this chat, use the second approach",
    "What did you change in this session?",
    "Same error as before, try again",
    "Why did the test fail? Fix it",
    "The endpoint returns 500 after the deploy, debug it",
    "Add a retry to the upload worker",
    "Receipt: pushed; we previously ran the suite, sha256 attached",
    "Read the A/B readout; we used to see higher yield",
    "Report only, no DB writes. Which commit is deployed?",
    "Here are the notes: " + "lorem ipsum " * 220 + " we used to have this",
    "",
]


def header(path: Path, **meta) -> str:
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "x", **meta}}) + "\n"
                    + json.dumps({"type": "response_item", "payload": {"text": "PRIVATE TURN"}}) + "\n")
    return str(path)


@pytest.fixture
def human(tmp_path):
    return header(tmp_path / "human.jsonl", thread_source="user", source="vscode")


@pytest.fixture
def e3_git(monkeypatch):
    run = Mock(return_value=types.SimpleNamespace(returncode=0, stdout="git@github.com:e3-solutions/sesh.git\n"))
    monkeypatch.setattr(sesh_context.subprocess, "run", run)
    return run


def call(transcript, tool=SESH_SEARCH, turn="turn-1", session="session-1"):
    return {"hook_event_name": "PreToolUse", "session_id": session, "turn_id": turn,
            "transcript_path": transcript, "tool_name": tool, "tool_input": {"query": "q"}}


def denied(output) -> bool:
    if output is None:
        return False
    specific = json.loads(output)["hookSpecificOutput"]
    assert specific == {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                        "permissionDecisionReason": need.DENY_REASON}
    return True


# One search per turn ---------------------------------------------------------------------------

@pytest.mark.parametrize("flag", OFF_VALUES)
def test_guard_off_allows_every_search_and_writes_nothing(tmp_path, human, flag):
    env = {} if flag is None else {"E3_SESH_ONE_PER_TURN": flag}
    for _ in range(3):
        assert need.one_per_turn_denial(call(human), env=env, directory=tmp_path / "g") is None
    assert not (tmp_path / "g").exists()


def test_guard_allows_first_search_denies_second_and_resets_next_turn(tmp_path, human):
    d = tmp_path / "g"
    assert need.one_per_turn_denial(call(human), env=GUARD_ON, directory=d) is None
    assert denied(need.one_per_turn_denial(call(human), env=GUARD_ON, directory=d))
    assert denied(need.one_per_turn_denial(call(human), env=GUARD_ON, directory=d))
    assert need.one_per_turn_denial(call(human, turn="turn-2"), env=GUARD_ON, directory=d) is None
    assert denied(need.one_per_turn_denial(call(human, turn="turn-2"), env=GUARD_ON, directory=d))
    # Only the current turn's marker is kept, and it holds no text.
    markers = [p for p in d.rglob("*") if p.is_file()]
    assert len(markers) == 1 and markers[0].read_bytes() == b""


@pytest.mark.parametrize("tool", [
    "mcp__e3_cosmos__sesh__search_coding_sessions", "mcp__cosmos__sesh__search_coding_sessions",
    "mcp__codex_apps__cosmos__sesh__search_coding_sessions", "search_coding_sessions",
])
def test_guard_covers_every_sesh_search_name(tmp_path, human, tool):
    d = tmp_path / "g"
    assert need.one_per_turn_denial(call(human, tool=tool), env=GUARD_ON, directory=d) is None
    assert denied(need.one_per_turn_denial(call(human, tool=tool), env=GUARD_ON, directory=d))


@pytest.mark.parametrize("tool", [
    "mcp__e3_cosmos__sesh__open_session_passages", "mcp__e3_cosmos__sesh__find_similar_sessions",
    "mcp__e3_cosmos__sesh__ask_sesh", "mcp__e3_cosmos__sesh__open_coding_session_source",
    "mcp__e3_cosmos__forum__search_research_posts", "Bash", None,
])
def test_guard_never_blocks_open_similar_ask_or_other_tools(tmp_path, human, tool):
    d = tmp_path / "g"
    assert need.one_per_turn_denial(call(human), env=GUARD_ON, directory=d) is None
    for _ in range(3):
        assert need.one_per_turn_denial(call(human, tool=tool), env=GUARD_ON, directory=d) is None


def test_guard_counts_each_session_separately(tmp_path, human):
    d = tmp_path / "g"
    assert need.one_per_turn_denial(call(human, session="a"), env=GUARD_ON, directory=d) is None
    assert need.one_per_turn_denial(call(human, session="b"), env=GUARD_ON, directory=d) is None
    assert denied(need.one_per_turn_denial(call(human, session="a"), env=GUARD_ON, directory=d))


@pytest.mark.parametrize("meta", [{"thread_source": "agent_created_thread"}, {"thread_source": "subagent"},
                                  {"source": "exec"}, {"source": {"subagent": "x"}}, {"originator": "codex_exec"}])
def test_guard_leaves_bots_unlimited(tmp_path, meta):
    bot = header(tmp_path / "bot.jsonl", **meta)
    for _ in range(3):
        assert need.one_per_turn_denial(call(bot), env=GUARD_ON, directory=tmp_path / "g") is None


def test_guard_respects_opt_out(tmp_path, human):
    env = {**GUARD_ON, "E3_SESH_EXPERIMENT_OPTOUT": "1"}
    for _ in range(3):
        assert need.one_per_turn_denial(call(human), env=env, directory=tmp_path / "g") is None


@pytest.mark.parametrize("change", [
    {"turn_id": None}, {"turn_id": "  "}, {"session_id": None}, {"transcript_path": None},
    {"transcript_path": "relative.jsonl"}, {"hook_event_name": "PostToolUse"},
])
def test_guard_allows_when_it_cannot_tell(tmp_path, human, change):
    item = {**call(human), **change}
    for _ in range(3):
        assert need.one_per_turn_denial(item, env=GUARD_ON, directory=tmp_path / "g") is None


@pytest.mark.parametrize("payload", [None, [], "x", {"tool_name": SESH_SEARCH, "turn_id": 5}])
def test_guard_fails_open_on_bad_payloads(tmp_path, payload):
    assert need.one_per_turn_denial(payload, env=GUARD_ON, directory=tmp_path / "g") is None


def test_guard_fails_open_when_state_cannot_be_written(tmp_path, human):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    for _ in range(3):
        assert need.one_per_turn_denial(call(human), env=GUARD_ON, directory=blocked / "g") is None


def test_parallel_searches_in_one_turn_let_exactly_one_through(tmp_path, human):
    results, start = [], threading.Barrier(8)

    def run():
        start.wait()
        results.append(need.one_per_turn_denial(call(human), env=GUARD_ON, directory=tmp_path / "g"))

    threads = [threading.Thread(target=run) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(result is None for result in results) == 1
    assert all(denied(result) for result in results if result is not None)


# Past-work cue ----------------------------------------------------------------------------------

def prompt(transcript, text, cwd="/repo"):
    return {"hook_event_name": "UserPromptSubmit", "session_id": "s", "turn_id": "t", "cwd": cwd,
            "transcript_path": transcript, "prompt": text}


@pytest.mark.parametrize("text", POSITIVE)
def test_cue_fires_on_questions_about_earlier_work(e3_git, human, text):
    assert need.asks_about_past_work(text)
    assert need.past_work_cue(prompt(human, text), env=CUE_ON) == need.PAST_WORK_CUE


@pytest.mark.parametrize("text", NEGATIVE)
def test_cue_stays_quiet_on_other_messages(e3_git, human, text):
    assert not need.asks_about_past_work(text)
    assert need.past_work_cue(prompt(human, text), env=CUE_ON) is None


def test_cue_is_one_line():
    assert "\n" not in need.PAST_WORK_CUE and len(need.PAST_WORK_CUE) < 200


@pytest.mark.parametrize("flag", OFF_VALUES)
def test_cue_off_reads_nothing_and_returns_output_unchanged(e3_git, human, flag, monkeypatch):
    env = {} if flag is None else {"E3_SESH_PAST_WORK_CUE": flag}
    reads = Mock(side_effect=AssertionError("transcript read while off"))
    monkeypatch.setattr(need, "transcript_header", reads)
    for output in (None, "", '{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "F"}}'):
        assert need.with_past_work_cue(output, prompt(human, POSITIVE[0]), env=env) is output
    e3_git.assert_not_called()


@pytest.mark.parametrize("meta", [{"thread_source": "agent_created_thread"}, {"source": "exec"},
                                  {"source": {"subagent": "x"}}])
def test_cue_skips_bots(e3_git, tmp_path, meta):
    bot = header(tmp_path / "bot.jsonl", **meta)
    assert need.past_work_cue(prompt(bot, POSITIVE[0]), env=CUE_ON) is None


@pytest.mark.parametrize("extra", [{"E3_SESH_EXPERIMENT_OPTOUT": "1"}, {"E3_SESH_CONTEXT_ENABLED": "0"},
                                   {"E3_SESH_START_SEARCH_ENABLED": "off"}])
def test_cue_skips_opt_outs_and_kill_switches(e3_git, human, extra):
    assert need.past_work_cue(prompt(human, POSITIVE[0]), env={**CUE_ON, **extra}) is None


def test_cue_skips_non_e3_repos_and_unreadable_threads(monkeypatch, tmp_path, human):
    other = Mock(return_value=types.SimpleNamespace(returncode=0, stdout="git@github.com:someone/else.git\n"))
    monkeypatch.setattr(sesh_context.subprocess, "run", other)
    assert need.past_work_cue(prompt(human, POSITIVE[0]), env=CUE_ON) is None
    monkeypatch.setattr(sesh_context.subprocess, "run",
                        Mock(return_value=types.SimpleNamespace(returncode=0, stdout="https://github.com/e3-solutions/x\n")))
    assert need.past_work_cue(prompt(human, POSITIVE[0]), env=CUE_ON) == need.PAST_WORK_CUE
    assert need.past_work_cue(prompt(str(tmp_path / "missing.jsonl"), POSITIVE[0]), env=CUE_ON) is None
    assert need.past_work_cue({**prompt(human, POSITIVE[0]), "hook_event_name": "Stop"}, env=CUE_ON) is None


def test_cue_joins_the_forum_cue_in_one_json_object(e3_git, human):
    forum = json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "FORUM"}})
    merged = json.loads(need.with_past_work_cue(forum, prompt(human, POSITIVE[0]), env=CUE_ON))
    assert merged == {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                             "additionalContext": "FORUM\n\n" + need.PAST_WORK_CUE}}
    alone = json.loads(need.with_past_work_cue(None, prompt(human, POSITIVE[0]), env=CUE_ON))
    assert alone["hookSpecificOutput"]["additionalContext"] == need.PAST_WORK_CUE
    assert need.with_past_work_cue("not json", prompt(human, POSITIVE[0]), env=CUE_ON) == "not json"


# Real hooks -------------------------------------------------------------------------------------

def hook_env(tmp_path: Path, extra: dict[str, str]) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("E3_SESH", "CODEX_SESSION_LOG", "E3_COLLECTIVE", "FORUM_CUE"))}
    exec_path = subprocess.run(["git", "--exec-path"], capture_output=True, text=True, check=True).stdout.strip()
    env.update({
        "PATH": os.pathsep.join([exec_path, env.get("PATH", "")]),
        "HOME": str(tmp_path / "home"),
        "CODEX_HOME": str(tmp_path / "codex-home"),
        "CODEX_SESSION_LOG_AUTO_UPLOAD": "0",
        "CODEX_SESSION_LOG_STATE_DIR": str(tmp_path / "state"),
        "E3_COLLECTIVE_FEEDBACK_HOOK_ENABLED": "0",
        "E3_COLLECTIVE_HOOK_ENABLED": "0",
        "FORUM_CUE_ENABLED": "0",
        **extra,
    })
    return env


def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    if not path.exists():
        path.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/e3-solutions/sesh.git"],
                       cwd=path, check=True)
    return path


def run_hook(tmp_path: Path, script: str, body: dict, extra: dict[str, str], scripts: Path = SCRIPTS):
    cwd = repo(tmp_path)
    return subprocess.run([sys.executable, str(scripts / script)], input=json.dumps({"cwd": str(cwd), **body}),
                          text=True, capture_output=True, env=hook_env(tmp_path, extra), cwd=cwd,
                          timeout=60, check=False)


def events(tmp_path: Path) -> list[dict]:
    path = tmp_path / "state/events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_pre_tool_use_hook_denies_the_second_search_and_marks_it(tmp_path):
    transcript = header(tmp_path / "r.jsonl", thread_source="user")
    first = run_hook(tmp_path, "pre_tool_use.py", call(transcript), GUARD_ON)
    second = run_hook(tmp_path, "pre_tool_use.py", call(transcript), GUARD_ON)
    opened = run_hook(tmp_path, "pre_tool_use.py",
                      call(transcript, tool="mcp__e3_cosmos__sesh__open_session_passages"), GUARD_ON)
    next_turn = run_hook(tmp_path, "pre_tool_use.py", call(transcript, turn="turn-2"), GUARD_ON)
    assert [r.returncode for r in (first, second, opened, next_turn)] == [0, 0, 0, 0]
    assert first.stdout == opened.stdout == next_turn.stdout == ""
    assert denied(second.stdout.strip())
    started = [e for e in events(tmp_path) if e["hook_event_name"] == "PreToolUse"]
    assert [e["metadata"].get("sesh_turn_guard") for e in started] == [None, "denied", None, None]


def test_user_prompt_submit_hook_adds_the_cue_only_when_on(tmp_path):
    transcript = header(tmp_path / "r.jsonl", thread_source="user")
    body = prompt(transcript, POSITIVE[2])
    del body["cwd"]
    off = run_hook(tmp_path / "off", "user_prompt_submit.py", body, {})
    on = run_hook(tmp_path / "on", "user_prompt_submit.py", body, CUE_ON)
    plain = run_hook(tmp_path / "plain", "user_prompt_submit.py", {**body, "prompt": NEGATIVE[2]}, CUE_ON)
    assert off.returncode == on.returncode == plain.returncode == 0
    assert off.stdout == plain.stdout == ""
    assert json.loads(on.stdout)["hookSpecificOutput"]["additionalContext"] == need.PAST_WORK_CUE
    # Nothing new is logged: the prompt event carries the same metadata keys either way.
    keys = [sorted(e["metadata"]) for path in ("off", "on") for e in events(tmp_path / path)
            if e["hook_event_name"] == "UserPromptSubmit"]
    assert keys and all(k == keys[0] for k in keys)


def test_user_prompt_submit_hook_merges_with_the_forum_cue(tmp_path):
    transcript = header(tmp_path / "r.jsonl", thread_source="user")
    body = prompt(transcript, POSITIVE[0])
    del body["cwd"]
    result = run_hook(tmp_path, "user_prompt_submit.py", body, {**CUE_ON, "FORUM_CUE_ENABLED": "1"})
    context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("Forum check") and context.endswith(need.PAST_WORK_CUE)


def _base_scripts(tmp_path: Path) -> Path | None:
    """The hook scripts exactly as they were on main before COR-4811, or None if git cannot show them."""
    try:
        archive = subprocess.run(
            ["git", "-C", str(ROOT), "archive", "--format=tar", BASE_COMMIT, "plugins/codex-session-logging/scripts"],
            capture_output=True, check=True, timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    with tarfile.open(fileobj=BytesIO(archive)) as tar:
        tar.extractall(tmp_path / "base", filter="data")
    return tmp_path / "base/plugins/codex-session-logging/scripts"


VOLATILE = {"id", "created_at", "seq", "thread_id", "content_path", "storage_path", "local_content_path"}


def _comparable(items: list[dict]) -> list[dict]:
    out = []
    for item in items:
        item = {k: v for k, v in item.items() if k not in VOLATILE}
        meta = {k: v for k, v in (item.get("metadata") or {}).items()
                if k not in {"plugin_version", "codex_setup", "hook_received_at", "captured_at"}}
        out.append({**item, "metadata": meta})
    return out


@pytest.mark.parametrize("flag", [None, "0", "off"])
def test_hooks_with_both_flags_off_match_the_previous_release(tmp_path, flag):
    base = _base_scripts(tmp_path)
    if base is None:
        pytest.skip(f"commit {BASE_COMMIT} is not available in this checkout")
    extra = {} if flag is None else {"E3_SESH_ONE_PER_TURN": flag, "E3_SESH_PAST_WORK_CUE": flag}
    for name, scripts in (("new", SCRIPTS), ("old", base)):
        work = tmp_path / name
        work.mkdir()
        transcript = header(work / "r.jsonl", thread_source="user")
        bot = header(work / "bot.jsonl", source="exec")
        bodies = [call(transcript), call(transcript), call(transcript, turn="turn-2"), call(bot), call(bot),
                  call(transcript, tool="Bash")]
        for text in POSITIVE[:4] + NEGATIVE[:4]:
            bodies.append({k: v for k, v in prompt(transcript, text).items() if k != "cwd"})
        outputs = []
        for body in bodies:
            script = "pre_tool_use.py" if body["hook_event_name"] == "PreToolUse" else "user_prompt_submit.py"
            for forum in ("0", "1"):
                result = run_hook(work / f"forum{forum}", script, body, {**extra, "FORUM_CUE_ENABLED": forum},
                                  scripts=scripts)
                outputs.append((result.returncode, result.stdout, result.stderr))
        logged = [_comparable(events(work / f"forum{forum}")) for forum in ("0", "1")]
        (work / "outputs.json").write_text(json.dumps(outputs).replace(str(work), "<W>"))
        (work / "events.json").write_text(json.dumps(logged).replace(str(work), "<W>"))
    assert (tmp_path / "new/outputs.json").read_text() == (tmp_path / "old/outputs.json").read_text()
    new_events = json.loads((tmp_path / "new/events.json").read_text())
    old_events = json.loads((tmp_path / "old/events.json").read_text())
    assert new_events == old_events
    assert any(new_events)
