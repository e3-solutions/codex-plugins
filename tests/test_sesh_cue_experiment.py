"""COR-4681: Sesh startup-cue on/off experiment (off by default)."""
from __future__ import annotations

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
sys.path.insert(0, str(SCRIPTS))
import sesh_context as context  # noqa: E402

E3_REMOTE = "git@github.com:e3-solutions/sesh.git\n"
EXPERIMENT_KEYS = {"sesh_cue_experiment", "sesh_cue_arm", "sesh_cue_salt"}
ON = {"E3_SESH_CUE_EXPERIMENT": "1"}


def uuid7(moment: datetime, rng: random.Random | None = None) -> str:
    rng = rng or random.Random()
    millis = int(moment.timestamp() * 1000)
    value = (millis << 80) | (0x7 << 76) | (rng.getrandbits(12) << 64) | (0b10 << 62) | rng.getrandbits(62)
    return str(uuid.UUID(int=value))


def session_with_arm(arm: str, moment: datetime) -> str:
    rng = random.Random(f"{arm}:{moment.isoformat()}")
    salt = context.iso_week_salt(moment)
    while True:
        session_id = uuid7(moment, rng)
        if context.assign_arm(salt, session_id) == arm:
            return session_id


def header(path: Path, **meta) -> str:
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "x", **meta}}) + "\n"
                    + json.dumps({"type": "response_item", "payload": {"text": "PRIVATE TURN"}}) + "\n")
    return str(path)


@pytest.fixture
def e3_git(monkeypatch):
    run = Mock(return_value=types.SimpleNamespace(returncode=0, stdout=E3_REMOTE))
    monkeypatch.setattr(context.subprocess, "run", run)
    return run


def payload(session_id, transcript, source="startup"):
    return {"hook_event_name": "SessionStart", "source": source, "cwd": "/repo",
            "session_id": session_id, "transcript_path": transcript}


MONDAY = datetime(2026, 10, 12, 16, 0, tzinfo=timezone.utc)  # 2026-W42


# Pure assignment ---------------------------------------------------------------------------

def test_assign_arm_is_deterministic_and_salted():
    session_id = "01a0c524-f3aa-7bbb-8ccc-0123456789ab"
    first = context.assign_arm("2026-W42", session_id)
    assert first in {"cue", "no_cue"}
    assert all(context.assign_arm("2026-W42", session_id) == first for _ in range(20))
    # Different weekly salts re-randomize the same id across many ids.
    ids = [uuid7(MONDAY, random.Random(n)) for n in range(400)]
    flips = sum(context.assign_arm("2026-W42", i) != context.assign_arm("2026-W43", i) for i in ids)
    assert 150 < flips < 250


def test_split_is_close_to_fifty_fifty_on_random_uuid7_ids():
    rng = random.Random(4681)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    ids = [uuid7(start + timedelta(seconds=rng.randrange(365 * 86400)), rng) for _ in range(40000)]
    arms = [context.assign_arm(context.week_salt(i), i) for i in ids]
    share = arms.count("cue") / len(arms)
    assert 0.49 < share < 0.51, share
    # Balanced within a single week too.
    week = [uuid7(MONDAY + timedelta(seconds=rng.randrange(6 * 86400)), rng) for _ in range(10000)]
    share = sum(context.assign_arm(context.week_salt(i), i) == "cue" for i in week) / len(week)
    assert 0.48 < share < 0.52, share


def test_week_is_derived_from_uuid7_timestamp():
    sunday_night = datetime(2026, 10, 11, 23, 59, 59, tzinfo=timezone.utc)  # last second of W41
    session_id = uuid7(sunday_night)
    assert context.uuid7_datetime(session_id) == sunday_night.replace(microsecond=0)
    # The session's own week wins over "now", so a later compaction keeps the salt.
    assert context.week_salt(session_id, now=MONDAY) == "2026-W41"
    assert context.week_salt(uuid7(datetime(2027, 1, 1, tzinfo=timezone.utc))) == "2026-W53"
    assert context.week_salt(uuid7(datetime(2026, 1, 1, tzinfo=timezone.utc))) == "2026-W01"


@pytest.mark.parametrize("session_id", [
    str(uuid.uuid4()), "not-a-uuid", "", None,
    "00000000-0000-7000-8000-000000000000",  # v7 shape but an implausible 1970 time
])
def test_week_falls_back_to_now_when_not_uuid7(session_id):
    assert context.uuid7_datetime(session_id) is None
    assert context.week_salt(session_id, now=MONDAY) == "2026-W42"


def test_salt_override_accepts_only_iso_weeks():
    session_id = uuid7(MONDAY)
    assert context.week_salt(session_id, override="2030-W07") == "2030-W07"
    for bad in ("", "anything", "2026-W60", "2026-W42 extra", "2026-w42"):
        assert context.week_salt(session_id, override=bad) == "2026-W42"


# Decision ----------------------------------------------------------------------------------

@pytest.mark.parametrize("flag", [None, "", "0", "false", "off", "yes", "enabled"])
@pytest.mark.parametrize("source", ["startup", "compact"])
def test_flag_off_is_unchanged(e3_git, tmp_path, flag, source):
    env = {} if flag is None else {"E3_SESH_CUE_EXPERIMENT": flag}
    no_cue_session = session_with_arm("no_cue", MONDAY)
    item = payload(no_cue_session, header(tmp_path / "r.jsonl", thread_source="user"), source)
    assert context.sesh_cue_decision(item, env=env) == (context.CONTEXT, None)
    # Non-boundary or foreign payloads stay silent exactly as before.
    assert context.sesh_cue_decision({**item, "source": "resume"}, env=env) == (None, None)


@pytest.mark.parametrize("flag", ["1", "true", "on", " TRUE "])
def test_flag_values_that_enable(e3_git, tmp_path, flag):
    item = payload(session_with_arm("no_cue", MONDAY), header(tmp_path / "r.jsonl", thread_source="user"))
    assert context.sesh_cue_decision(item, env={"E3_SESH_CUE_EXPERIMENT": flag}) == (
        None, {"sesh_cue_experiment": "sesh_cue_v1", "sesh_cue_arm": "no_cue", "sesh_cue_salt": "2026-W42"})


@pytest.mark.parametrize("arm", ["cue", "no_cue"])
def test_flag_on_randomizes_human_sessions(e3_git, tmp_path, arm):
    item = payload(session_with_arm(arm, MONDAY), header(tmp_path / "r.jsonl", thread_source="user",
                                                          source="vscode", originator="Codex Desktop"))
    cue, metadata = context.sesh_cue_decision(item, env=ON)
    assert cue == (context.CONTEXT if arm == "cue" else None)
    assert metadata == {"sesh_cue_experiment": "sesh_cue_v1", "sesh_cue_arm": arm, "sesh_cue_salt": "2026-W42"}
    assert "PRIVATE" not in json.dumps(metadata)


@pytest.mark.parametrize("arm", ["cue", "no_cue"])
def test_compaction_keeps_the_startup_arm_across_a_week_boundary(e3_git, tmp_path, arm):
    sunday_night = datetime(2026, 10, 11, 23, 30, tzinfo=timezone.utc)
    session_id = session_with_arm(arm, sunday_night)
    transcript = header(tmp_path / "r.jsonl", thread_source="user")
    start = context.sesh_cue_decision(payload(session_id, transcript, "startup"), env=ON, now=sunday_night)
    later = sunday_night + timedelta(days=3)
    compact = context.sesh_cue_decision(payload(session_id, transcript, "compact"), env=ON, now=later)
    assert start == compact
    assert compact[1]["sesh_cue_arm"] == arm
    assert compact[1]["sesh_cue_salt"] == "2026-W41"


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_optout_always_gets_the_cue(e3_git, tmp_path, value):
    item = payload(session_with_arm("no_cue", MONDAY), header(tmp_path / "r.jsonl", thread_source="user"))
    cue, metadata = context.sesh_cue_decision(item, env={**ON, "E3_SESH_EXPERIMENT_OPTOUT": value})
    assert cue == context.CONTEXT
    assert metadata["sesh_cue_arm"] == "optout"


@pytest.mark.parametrize("value", ["", "0", "false", "off"])
def test_falsy_optout_is_not_an_optout(e3_git, tmp_path, value):
    item = payload(session_with_arm("no_cue", MONDAY), header(tmp_path / "r.jsonl", thread_source="user"))
    assert context.sesh_cue_decision(item, env={**ON, "E3_SESH_EXPERIMENT_OPTOUT": value})[1]["sesh_cue_arm"] == "no_cue"


@pytest.mark.parametrize("meta", [
    {"thread_source": "agent_created_thread", "source": "vscode"},
    {"thread_source": "agent_forked_thread", "source": "vscode"},
    {"thread_source": "guardian_review", "source": {"subagent": {"other": "guardian"}}},
    {"thread_source": "subagent", "source": {"subagent": {"thread_spawn": {"parent_thread_id": "p"}}}},
    {"thread_source": "vm_thread_tools"},
    {"thread_source": "automation"},
    {"thread_source": "user", "source": "exec"},
    {"source": "exec"},
    {"source": "cli", "originator": "codex_exec"},
])
@pytest.mark.parametrize("arm", ["cue", "no_cue"])
def test_bots_always_get_the_cue(e3_git, tmp_path, meta, arm):
    item = payload(session_with_arm(arm, MONDAY), header(tmp_path / "r.jsonl", **meta))
    cue, metadata = context.sesh_cue_decision(item, env=ON)
    assert cue == context.CONTEXT
    assert metadata["sesh_cue_arm"] == "bot"


@pytest.mark.parametrize("meta", [{"thread_source": "user", "source": "cli"}, {"source": "vscode"}, {}])
def test_human_threads_are_randomized(e3_git, tmp_path, meta):
    item = payload(session_with_arm("no_cue", MONDAY), header(tmp_path / "r.jsonl", **meta))
    assert context.sesh_cue_decision(item, env=ON)[1]["sesh_cue_arm"] == "no_cue"


def test_unknown_thread_or_session_keeps_the_cue(e3_git, tmp_path):
    session_id = session_with_arm("no_cue", MONDAY)
    (tmp_path / "bad.jsonl").write_text("not json\n")
    (tmp_path / "list.jsonl").write_text("[1, 2]\n")
    (tmp_path / "other.jsonl").write_text(json.dumps({"type": "response_item", "payload": {}}) + "\n")
    for item in (
        payload(session_id, None),
        payload(session_id, str(tmp_path / "missing.jsonl")),
        payload(session_id, "relative.jsonl"),
        payload(session_id, str(tmp_path / "bad.jsonl")),
        payload(session_id, str(tmp_path / "list.jsonl")),
        payload(session_id, str(tmp_path)),
        payload(session_id, str(tmp_path / "other.jsonl")),
        payload(None, header(tmp_path / "r.jsonl", thread_source="user")),
    ):
        cue, metadata = context.sesh_cue_decision(item, env=ON)
        assert cue == context.CONTEXT
        assert metadata["sesh_cue_arm"] == "unassigned"


@pytest.mark.parametrize("name", ["E3_SESH_CONTEXT_ENABLED", "E3_SESH_START_SEARCH_ENABLED"])
@pytest.mark.parametrize("extra", [{}, {"E3_SESH_EXPERIMENT_OPTOUT": "1"}])
@pytest.mark.parametrize("flag", [{}, ON])
def test_disable_env_still_wins(monkeypatch, tmp_path, name, extra, flag):
    monkeypatch.setattr(context.subprocess, "run", Mock(side_effect=AssertionError("must not spawn")))
    item = payload(session_with_arm("cue", MONDAY), header(tmp_path / "r.jsonl", thread_source="agent_created_thread"))
    cue, metadata = context.sesh_cue_decision(item, env={name: "0", **extra, **flag})
    assert cue is None
    assert metadata == ({"sesh_cue_experiment": "sesh_cue_v1", "sesh_cue_arm": "disabled",
                         "sesh_cue_salt": "2026-W42"} if flag else None)


def test_foreign_repository_or_boundary_records_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(context.subprocess, "run", Mock(return_value=types.SimpleNamespace(
        returncode=0, stdout="https://github.com/other/repo\n")))
    item = payload(session_with_arm("cue", MONDAY), header(tmp_path / "r.jsonl", thread_source="user"))
    assert context.sesh_cue_decision(item, env=ON) == (None, None)
    for source in ("resume", "clear", None):
        assert context.sesh_cue_decision({**item, "source": source}, env=ON) == (None, None)


def test_sesh_context_entrypoint_follows_the_decision(e3_git, tmp_path, monkeypatch):
    item = payload(session_with_arm("no_cue", MONDAY), header(tmp_path / "r.jsonl", thread_source="user"))
    monkeypatch.delenv("E3_SESH_CUE_EXPERIMENT", raising=False)
    monkeypatch.delenv("E3_SESH_CONTEXT_ENABLED", raising=False)
    monkeypatch.delenv("E3_SESH_START_SEARCH_ENABLED", raising=False)
    assert context.sesh_context(item) == context.CONTEXT
    monkeypatch.setenv("E3_SESH_CUE_EXPERIMENT", "1")
    assert context.sesh_context(item) is None


# Real SessionStart hook -------------------------------------------------------------------

def run_session_start(tmp_path: Path, session_id: str, env_extra: dict[str, str], *, source="startup"):
    repo = tmp_path / "repo"
    if not repo.exists():
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/e3-solutions/sesh.git"],
                       cwd=repo, check=True)
    transcript = header(tmp_path / f"rollout-{session_id}.jsonl", thread_source="user",
                        source="vscode", originator="Codex Desktop")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("E3_SESH", "CODEX_SESSION_LOG", "E3_COLLECTIVE"))}
    env.update({
        "HOME": str(tmp_path / "home"),
        "CODEX_HOME": str(tmp_path / "codex-home"),
        "CODEX_SESSION_LOG_AUTO_UPLOAD": "0",
        "CODEX_SESSION_LOG_STATE_DIR": str(tmp_path / "state"),
        "E3_COLLECTIVE_FEEDBACK_HOOK_ENABLED": "0",
        "E3_COLLECTIVE_HOOK_ENABLED": "0",
        **env_extra,
    })
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "session_start.py")],
        input=json.dumps({"hook_event_name": "SessionStart", "source": source, "cwd": str(repo),
                          "session_id": session_id, "transcript_path": transcript}),
        text=True, capture_output=True, env=env, cwd=repo, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    events = [json.loads(line) for line in (tmp_path / "state/events.jsonl").read_text().splitlines()]
    starts = [event for event in events if event["hook_event_name"] == "SessionStart"
              and event["event_type"] == "environment_snapshot"]
    return result.stdout, starts[-1]


def test_hook_flag_off_prints_cue_and_logs_no_arm(tmp_path):
    stdout, event = run_session_start(tmp_path, session_with_arm("no_cue", MONDAY), {})
    assert context.CONTEXT in stdout
    assert EXPERIMENT_KEYS.isdisjoint(event["metadata"])
    assert event["event_type"] == "environment_snapshot"


@pytest.mark.parametrize("arm", ["cue", "no_cue"])
def test_hook_flag_on_logs_arm_on_session_start_and_compaction(tmp_path, arm):
    session_id = session_with_arm(arm, MONDAY)
    for source in ("startup", "compact"):
        stdout, event = run_session_start(tmp_path, session_id, ON, source=source)
        assert (context.CONTEXT in stdout) is (arm == "cue")
        assert {key: event["metadata"][key] for key in EXPERIMENT_KEYS} == {
            "sesh_cue_experiment": "sesh_cue_v1", "sesh_cue_arm": arm, "sesh_cue_salt": "2026-W42"}
        # The queued upload record carries the same metadata as the local log.
        detail = json.loads((tmp_path / "state" / event["local_content_path"]).read_text())
        assert detail["metadata"] == event["metadata"]
        assert "PRIVATE TURN" not in json.dumps(detail)


def test_hook_disable_env_logs_disabled_without_cue(tmp_path):
    stdout, event = run_session_start(tmp_path, session_with_arm("cue", MONDAY),
                                      {**ON, "E3_SESH_CONTEXT_ENABLED": "0"})
    assert context.CONTEXT not in stdout
    assert event["metadata"]["sesh_cue_arm"] == "disabled"
