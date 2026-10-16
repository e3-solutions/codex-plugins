"""COR-4808: "fewer routine searches" A/B behind E3_SESH_ROUTINE_AB (off by default)."""
from __future__ import annotations

import hashlib
import importlib.util
import itertools
import json
import os
import random
import subprocess
import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/codex-session-logging/scripts"
SKILL = ROOT / "plugins/codex-session-logging/skills/search-coding-sessions/SKILL.md"
sys.path.insert(0, str(SCRIPTS))
import sesh_context as context  # noqa: E402

E3_REMOTE = "git@github.com:e3-solutions/sesh.git\n"
ON = {"E3_SESH_ROUTINE_AB": "1"}
BASE_COMMIT = "d1ee3fb"  # origin/main before COR-4808
MONDAY = datetime(2026, 10, 12, 16, 0, tzinfo=timezone.utc)
SESH_TOOL = "mcp__e3_cosmos__sesh__search_coding_sessions"
REQUEST_ID = "11111111-1111-4111-8111-111111111111"


def uuid7(moment: datetime, rng: random.Random) -> str:
    millis = int(moment.timestamp() * 1000)
    value = (millis << 80) | (0x7 << 76) | (rng.getrandbits(12) << 64) | (0b10 << 62) | rng.getrandbits(62)
    return str(uuid.UUID(int=value))


def session_in(arm: str, moment: datetime = MONDAY, seed: int = 0) -> str:
    rng = random.Random(f"{arm}:{seed}")
    while True:
        session_id = uuid7(moment, rng)
        if context.routine_arm(session_id) == arm:
            return session_id


def header(path: Path, **meta) -> str:
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "x", **meta}}) + "\n"
                    + json.dumps({"type": "response_item", "payload": {"text": "PRIVATE TURN"}}) + "\n")
    return str(path)


def payload(session_id, transcript, source="startup"):
    return {"hook_event_name": "SessionStart", "source": source, "cwd": "/repo",
            "session_id": session_id, "transcript_path": transcript}


@pytest.fixture
def e3_git(monkeypatch):
    run = Mock(return_value=types.SimpleNamespace(returncode=0, stdout=E3_REMOTE))
    monkeypatch.setattr(context.subprocess, "run", run)
    return run


# Arm assignment ------------------------------------------------------------------------------

def test_arm_is_a_pure_function_of_the_session_id():
    session_id = "019a0c52-4f3a-7bbb-8ccc-0123456789ab"
    arm = context.routine_arm(session_id)
    assert arm in {"routine", "need_only"}
    assert all(context.routine_arm(session_id) == arm for _ in range(20))
    # Case and surrounding whitespace do not matter (ids are compared lowercased).
    assert context.routine_arm(f"  {session_id.upper()}\n") == arm


def test_arm_matches_the_documented_offline_formula():
    # Offline: arm = ("routine", "need_only")[sha256("sesh_routine_v1:" + lower(trim(id)))[0] % 2].
    # Postgres: get_byte(sha256(convert_to('sesh_routine_v1:' || lower(btrim(id)), 'UTF8')), 0) % 2.
    rng = random.Random(4808)
    for _ in range(2000):
        session_id = str(uuid.UUID(int=rng.getrandbits(128)))
        first_byte = int(hashlib.sha256(f"sesh_routine_v1:{session_id}".encode()).hexdigest()[:2], 16)
        assert context.routine_arm(session_id) == ("routine", "need_only")[first_byte % 2]


def test_golden_arms_never_change():
    # Pinned so a refactor cannot silently re-randomize sessions mid-experiment.
    assert {sid: context.routine_arm(sid) for sid in GOLDEN} == GOLDEN


GOLDEN = {
    "019a0c52-4f3a-7bbb-8ccc-0123456789ab": "routine",
    "019a1f00-0000-7000-8000-000000000001": "routine",
    "019a1f00-0000-7000-8000-000000000003": "need_only",
    "019a1f00-0000-7000-8000-000000000006": "need_only",
    "019a1f00-0000-7000-8000-000000000007": "need_only",
    "0199e5a2-6c41-7d2e-9f10-5b8c3a7e4d21": "routine",
    "3f2504e0-4f89-41d3-9a0c-0305e82c3301": "routine",
}


@pytest.mark.parametrize("kind", ["uuid7", "uuid4"])
def test_split_is_close_to_fifty_fifty(kind):
    rng = random.Random(f"split-{kind}")
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    if kind == "uuid7":
        ids = [uuid7(start + timedelta(seconds=rng.randrange(365 * 86400)), rng) for _ in range(40000)]
    else:
        ids = [str(uuid.UUID(int=rng.getrandbits(128), version=4)) for _ in range(40000)]
    share = sum(context.routine_arm(i) == "need_only" for i in ids) / len(ids)
    assert 0.49 < share < 0.51, share
    # Balanced inside a single day too: there is no time or week salt.
    day = [uuid7(MONDAY + timedelta(seconds=rng.randrange(86400)), rng) for _ in range(10000)]
    share = sum(context.routine_arm(i) == "need_only" for i in day) / len(day)
    assert 0.48 < share < 0.52, share


def test_arm_is_independent_of_the_cor4681_arm():
    rng = random.Random(4681)
    ids = [uuid7(MONDAY + timedelta(seconds=rng.randrange(6 * 86400)), rng) for _ in range(20000)]
    cells = {}
    for i in ids:
        key = (context.routine_arm(i), context.assign_arm(context.week_salt(i), i))
        cells[key] = cells.get(key, 0) + 1
    assert len(cells) == 4
    assert all(0.23 < count / len(ids) < 0.27 for count in cells.values()), cells


# Flag off: byte-identical to today ----------------------------------------------------------------

OFF_VALUES = [None, "", "0", "false", "off", "no", "yes", "enabled"]


@pytest.mark.parametrize("flag", OFF_VALUES)
def test_flag_off_never_withholds_and_reads_nothing(monkeypatch, tmp_path, flag):
    reads = Mock(side_effect=AssertionError("must not read the transcript"))
    monkeypatch.setattr(context, "transcript_header", reads)
    env = {} if flag is None else {"E3_SESH_ROUTINE_AB": flag}
    item = payload(session_in("need_only"), str(tmp_path / "r.jsonl"))
    assert context.routine_cue_withheld(item, env) is False
    reads.assert_not_called()


@pytest.mark.parametrize("flag", OFF_VALUES)
@pytest.mark.parametrize("arm", ["routine", "need_only"])
def test_flag_off_keeps_todays_cue(e3_git, tmp_path, flag, arm):
    env = {} if flag is None else {"E3_SESH_ROUTINE_AB": flag}
    item = payload(session_in(arm), header(tmp_path / "r.jsonl", thread_source="user"))
    assert context.sesh_cue_decision(item, env=env) == (context.CONTEXT, None)
    assert context.sesh_cue_decision({**item, "source": "compact"}, env=env) == (None, None)
    assert context.sesh_cue_decision({**item, "source": "compact"},
                                     env={**env, "E3_SESH_CUE_AFTER_COMPACTION": "1"}) == (
        context.COMPACTION_CONTEXT, None)


def _base_module():
    """sesh_context.py exactly as it was on main before COR-4808, or None when git cannot show it."""
    try:
        source = subprocess.run(
            ["git", "-C", str(ROOT), "show", f"{BASE_COMMIT}:plugins/codex-session-logging/scripts/sesh_context.py"],
            capture_output=True, text=True, check=True, timeout=20,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    spec = importlib.util.spec_from_loader("sesh_context_base", loader=None)
    module = importlib.util.module_from_spec(spec)
    exec(compile(source, "sesh_context_base.py", "exec"), module.__dict__)  # noqa: S102 - trusted repo file
    return module


def test_flag_off_matches_the_previous_release_exactly(monkeypatch, tmp_path):
    base = _base_module()
    if base is None:
        pytest.skip(f"commit {BASE_COMMIT} is not available in this checkout")
    for module in (context, base):
        monkeypatch.setattr(module.subprocess, "run",
                            Mock(return_value=types.SimpleNamespace(returncode=0, stdout=E3_REMOTE)))
    transcripts = [
        header(tmp_path / "user.jsonl", thread_source="user", source="vscode"),
        header(tmp_path / "bot.jsonl", thread_source="agent_created_thread"),
        header(tmp_path / "exec.jsonl", source="exec"),
        str(tmp_path / "missing.jsonl"),
        None,
    ]
    sessions = [session_in("routine"), session_in("need_only"), session_in("need_only", seed=1),
                str(uuid.uuid4()), "not-a-uuid", None]
    sources = ["startup", "compact", "resume", "clear", None]
    others = [{}, {"E3_SESH_CUE_AFTER_COMPACTION": "1"}, {"E3_SESH_CUE_EXPERIMENT": "1"},
              {"E3_SESH_CUE_EXPERIMENT": "1", "E3_SESH_EXPERIMENT_OPTOUT": "1"},
              {"E3_SESH_CONTEXT_ENABLED": "0"}, {"E3_SESH_START_SEARCH_ENABLED": "off"}]
    checked = 0
    for transcript, session_id, source, other, flag in itertools.product(
            transcripts, sessions, sources, others, OFF_VALUES):
        env = dict(other) if flag is None else {**other, "E3_SESH_ROUTINE_AB": flag}
        item = payload(session_id, transcript, source)
        assert context.sesh_cue_decision(item, env=env, now=MONDAY) == base.sesh_cue_decision(
            item, env=env, now=MONDAY), (transcript, session_id, source, env)
        checked += 1
    assert checked == 5 * 6 * 5 * 6 * len(OFF_VALUES)


# Flag on --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["1", "true", "on", " TRUE "])
def test_values_that_turn_it_on(e3_git, tmp_path, value):
    item = payload(session_in("need_only"), header(tmp_path / "r.jsonl", thread_source="user"))
    assert context.sesh_cue_decision(item, env={"E3_SESH_ROUTINE_AB": value}) == (None, None)


@pytest.mark.parametrize("meta", [{"thread_source": "user", "source": "vscode", "originator": "Codex Desktop"},
                                  {"thread_source": "user", "source": "cli"}, {"source": "vscode"}, {}])
def test_need_only_human_sessions_get_no_routine_cue(e3_git, tmp_path, meta):
    item = payload(session_in("need_only"), header(tmp_path / "r.jsonl", **meta))
    assert context.sesh_cue_decision(item, env=ON) == (None, None)
    assert context.sesh_cue_decision({**item, "source": "compact"},
                                     env={**ON, "E3_SESH_CUE_AFTER_COMPACTION": "1"}) == (None, None)
    assert context.sesh_context(item) == context.CONTEXT  # process env has the flag off


def test_routine_sessions_keep_todays_cues(e3_git, tmp_path):
    item = payload(session_in("routine"), header(tmp_path / "r.jsonl", thread_source="user"))
    assert context.sesh_cue_decision(item, env=ON) == (context.CONTEXT, None)
    assert context.sesh_cue_decision({**item, "source": "compact"}, env=ON) == (None, None)
    assert context.sesh_cue_decision({**item, "source": "compact"},
                                     env={**ON, "E3_SESH_CUE_AFTER_COMPACTION": "1"}) == (
        context.COMPACTION_CONTEXT, None)


@pytest.mark.parametrize("arm", ["routine", "need_only"])
def test_arm_is_the_same_at_startup_compaction_and_any_later_time(e3_git, tmp_path, arm):
    session_id = session_in(arm, datetime(2026, 10, 11, 23, 59, tzinfo=timezone.utc))
    transcript = header(tmp_path / "r.jsonl", thread_source="user")
    env = {**ON, "E3_SESH_CUE_AFTER_COMPACTION": "1"}
    for days in (0, 1, 8, 400):
        now = MONDAY + timedelta(days=days)
        start, _ = context.sesh_cue_decision(payload(session_id, transcript), env=env, now=now)
        compact, _ = context.sesh_cue_decision(payload(session_id, transcript, "compact"), env=env, now=now)
        assert (start is None, compact is None) == ((arm == "need_only"),) * 2


@pytest.mark.parametrize("meta", [
    {"thread_source": "agent_created_thread", "source": "vscode"},
    {"thread_source": "guardian_review", "source": {"subagent": {"other": "guardian"}}},
    {"thread_source": "subagent", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "p"}}}},
    {"thread_source": "automation"},
    {"source": "exec"},
    {"source": "cli", "originator": "codex_exec"},
])
def test_bots_keep_the_cue_in_both_arms(e3_git, tmp_path, meta):
    for arm in ("routine", "need_only"):
        item = payload(session_in(arm), header(tmp_path / "r.jsonl", **meta))
        assert context.sesh_cue_decision(item, env=ON) == (context.CONTEXT, None)


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_optout_keeps_the_cue(e3_git, tmp_path, value):
    item = payload(session_in("need_only"), header(tmp_path / "r.jsonl", thread_source="user"))
    assert context.sesh_cue_decision(item, env={**ON, "E3_SESH_EXPERIMENT_OPTOUT": value}) == (context.CONTEXT, None)


def test_unreadable_thread_or_id_keeps_the_cue(e3_git, tmp_path):
    session_id = session_in("need_only")
    (tmp_path / "bad.jsonl").write_text("not json\n")
    for item in (payload(session_id, None), payload(session_id, str(tmp_path / "missing.jsonl")),
                 payload(session_id, "relative.jsonl"), payload(session_id, str(tmp_path / "bad.jsonl")),
                 payload(None, header(tmp_path / "r.jsonl", thread_source="user"))):
        assert context.sesh_cue_decision(item, env=ON) == (context.CONTEXT, None)


@pytest.mark.parametrize("name", ["E3_SESH_CONTEXT_ENABLED", "E3_SESH_START_SEARCH_ENABLED"])
def test_kill_switch_still_wins(monkeypatch, tmp_path, name):
    monkeypatch.setattr(context.subprocess, "run", Mock(side_effect=AssertionError("must not spawn")))
    for arm in ("routine", "need_only"):
        item = payload(session_in(arm), header(tmp_path / "r.jsonl", thread_source="user"))
        assert context.sesh_cue_decision(item, env={**ON, name: "0"}) == (None, None)


def test_foreign_repository_and_other_boundaries_stay_silent(monkeypatch, tmp_path):
    monkeypatch.setattr(context.subprocess, "run", Mock(return_value=types.SimpleNamespace(
        returncode=0, stdout="https://github.com/other/repo\n")))
    item = payload(session_in("routine"), header(tmp_path / "r.jsonl", thread_source="user"))
    assert context.sesh_cue_decision(item, env=ON) == (None, None)
    for source in ("resume", "clear", None):
        assert context.sesh_cue_decision({**item, "source": source}, env=ON) == (None, None)


@pytest.mark.parametrize("arms", list(itertools.product(["routine", "need_only"], ["cue", "no_cue"])))
def test_with_cor4681_also_on_a_session_gets_the_cue_only_when_both_arms_allow_it(e3_git, tmp_path, arms):
    routine, cue_arm = arms
    rng = random.Random(f"{routine}:{cue_arm}")
    while True:
        session_id = uuid7(MONDAY, rng)
        if context.routine_arm(session_id) == routine and context.assign_arm("2026-W42", session_id) == cue_arm:
            break
    item = payload(session_id, header(tmp_path / "r.jsonl", thread_source="user"))
    cue, metadata = context.sesh_cue_decision(item, env={**ON, "E3_SESH_CUE_EXPERIMENT": "1"})
    assert cue == (context.CONTEXT if (routine, cue_arm) == ("routine", "cue") else None)
    # The COR-4681 log is its own arm only; COR-4808 adds no logging.
    assert metadata == {"sesh_cue_experiment": "sesh_cue_v1", "sesh_cue_arm": cue_arm, "sesh_cue_salt": "2026-W42"}


# Real hooks: need_only gets no cue, and Sesh search still works and is still linked -------------------

def hook_env(tmp_path: Path, extra: dict[str, str]) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("E3_SESH", "CODEX_SESSION_LOG", "E3_COLLECTIVE", "FORUM_CUE"))}
    # Call the real git binary: the macOS xcrun shim can eat most of the hook's 0.5 s git budget.
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
        path.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=path, check=True)
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/e3-solutions/sesh.git"],
                       cwd=path, check=True)
    return path


def run_hook(tmp_path: Path, script: str, body: dict, extra: dict[str, str]) -> subprocess.CompletedProcess:
    cwd = repo(tmp_path)
    result = subprocess.run([sys.executable, str(SCRIPTS / script)], input=json.dumps({"cwd": str(cwd), **body}),
                            text=True, capture_output=True, env=hook_env(tmp_path, extra), cwd=cwd,
                            timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    return result


def events(tmp_path: Path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "state/events.jsonl").read_text().splitlines()]


def start(tmp_path: Path, session_id: str, extra: dict[str, str]):
    tmp_path.mkdir(parents=True, exist_ok=True)
    transcript = header(tmp_path / f"rollout-{session_id}.jsonl", thread_source="user",
                        source="vscode", originator="Codex Desktop")
    result = run_hook(tmp_path, "session_start.py", {"hook_event_name": "SessionStart", "source": "startup",
                                                     "session_id": session_id, "transcript_path": transcript}, extra)
    snapshot = [event for event in events(tmp_path) if event["hook_event_name"] == "SessionStart"][-1]
    return result.stdout, transcript, snapshot


@pytest.mark.parametrize("arm", ["routine", "need_only"])
def test_hook_flag_off_is_identical_for_both_arms(tmp_path, arm):
    default, _, default_event = start(tmp_path / "default", session_in(arm), {})
    off, _, off_event = start(tmp_path / "off", session_in(arm), {"E3_SESH_ROUTINE_AB": "0"})
    assert default == off
    assert context.CONTEXT in default
    assert set(default_event["metadata"]) == set(off_event["metadata"])


@pytest.mark.parametrize("arm", ["routine", "need_only"])
def test_hook_flag_on_withholds_only_the_need_only_cue_and_logs_nothing_new(tmp_path, arm):
    stdout, _, event = start(tmp_path / "on", session_in(arm), ON)
    _, _, baseline = start(tmp_path / "off", session_in(arm), {})
    assert (context.CONTEXT in stdout) is (arm == "routine")
    assert "Sesh prior-work context" not in stdout or arm == "routine"
    assert set(event["metadata"]) == set(baseline["metadata"])
    assert not any("routine" in key for key in event["metadata"])


def test_need_only_session_can_still_search_and_the_search_is_still_linked(tmp_path):
    session_id = session_in("need_only")
    stdout, transcript, _ = start(tmp_path, session_id, ON)
    assert "Sesh prior-work context" not in stdout
    call = {"session_id": session_id, "transcript_path": transcript, "tool_name": SESH_TOOL,
            "tool_input": {"query": "why did the GPU remap drop sessions"}}
    pre = run_hook(tmp_path, "pre_tool_use.py", {**call, "hook_event_name": "PreToolUse"}, ON)
    # No deny, no rewritten input: the call goes to Sesh exactly as the agent wrote it.
    assert pre.stdout == ""
    answer = {"retrieval_status": "candidates_found", "results": [], "search_request_id": REQUEST_ID}
    post = run_hook(tmp_path, "post_tool_use.py", {
        **call, "hook_event_name": "PostToolUse",
        "tool_response": {"content": [{"type": "text", "text": json.dumps(answer)}], "isError": False},
    }, ON)
    assert post.stdout == ""
    linked = [event for event in events(tmp_path) if event["hook_event_name"] == "PostToolUse"]
    assert linked and linked[-1]["metadata"]["sesh_request_id"] == REQUEST_ID
    assert linked[-1]["session_id"] == session_id  # the id the readout hashes into an arm


def test_the_need_based_skill_still_ships_and_never_asks_for_a_routine_search():
    text = SKILL.read_text()
    description = text.split("description:", 1)[1].split("\n", 1)[0]
    assert "when a user asks what was tried before" in description
    assert "do not require a search before unrelated work" in text
    manifest = json.loads((ROOT / "plugins/codex-session-logging/.jollyroger.json").read_text())
    assert {"path": "skills/search-coding-sessions", "providers": ["codex", "claude"]} in manifest["skills"]
