"""COR-4884: the standalone `sesh` plugin.

It carries the Sesh hooks from codex-session-logging 0.2.38 (38f2466) unchanged: the startup
cue with the routine A/B, the past-work cue, the one-search-per-turn guard and Sesh search
linking. Alone it does all of that; next to an active codex-session-logging it does nothing,
so no hook fires twice.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LOGGER = ROOT / "plugins/codex-session-logging"
SESH = ROOT / "plugins/sesh"
SESH_SCRIPTS = SESH / "scripts"
LOGGER_SCRIPTS = LOGGER / "scripts"
sys.path.insert(0, str(SESH_SCRIPTS))
import sesh_plugin  # noqa: E402


def _vendored_logger():
    """This plugin's session_logging.py, loaded under its own name (other tests import the logger's)."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("sesh_vendored_session_logging",
                                                  SESH_SCRIPTS / "session_logging.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

SEARCH = "mcp__e3_cosmos__sesh__search_coding_sessions"
OPEN = "mcp__e3_cosmos__sesh__open_coding_session_source"
REQUEST_ID = "0b6f7c1e-3d2a-4c5b-9e8f-1a2b3c4d5e6f"
HUMAN_ROUTINE = "019a0c52-0000-7000-8000-000000000001"
EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse")
PAST_WORK = "Which change hardcoded the routing flag to true?"


def _session(arm: str) -> str:
    import sesh_context

    for index in range(1000):
        candidate = f"019a0c52-0000-7000-8000-{index:012d}"
        if sesh_context.routine_arm(candidate) == arm:
            return candidate
    raise AssertionError(arm)


ROUTINE = _session("routine")
NEED_ONLY = _session("need_only")


# Packaging ---------------------------------------------------------------------------------------

def _manifest() -> dict:
    return json.loads((SESH / ".codex-plugin/plugin.json").read_text())


def test_versions_agree_everywhere():
    session_logging = _vendored_logger()

    version = _manifest()["version"]
    assert _manifest()["name"] == "sesh"
    assert json.loads((SESH / ".jollyroger.json").read_text())["version"] == version
    assert session_logging.PLUGIN_NAME == "sesh" and session_logging.PLUGIN_VERSION == version


def test_version_never_reuses_a_logger_version():
    # Draft #92 and later logger releases keep the 0.2.x line; this plugin starts its own.
    assert not _manifest()["version"].startswith("0.2.")


def test_hooks_point_at_this_plugins_scripts():
    hooks = json.loads((SESH / "hooks/hooks.json").read_text())["hooks"]
    assert sorted(hooks) == sorted(EVENTS)
    for event, entries in hooks.items():
        [entry] = entries
        [hook] = entry["hooks"]
        script = re.search(r"cache/\*/sesh/\*/scripts/([a-z_]+\.py)", hook["command"]).group(1)
        assert (SESH_SCRIPTS / script).is_file(), event
        assert hook["command"] == json.loads((LOGGER / "hooks/hooks.json").read_text())["hooks"][event][0][
            "hooks"][0]["command"].replace("/codex-session-logging/", "/sesh/")


def test_jollyroger_include_list_covers_every_file_a_hook_imports():
    include = json.loads((SESH / ".jollyroger.json").read_text())["include"]
    scripts = sorted(f"scripts/{path.name}" for path in SESH_SCRIPTS.glob("*.py"))
    assert sorted(item for item in include if item.startswith("scripts/")) == scripts
    for item in include:
        assert (SESH / item).exists(), item


def test_fits_the_jolly_roger_native_plugin_payload_rules():
    # jolly-roger #10: regular files only, mode 100644/100755, at most 512 files and 8 MiB.
    files = [path for path in SESH.rglob("*") if "__pycache__" not in path.parts and not path.is_dir()]
    assert 0 < len(files) <= 512
    assert sum(path.stat().st_size for path in files) <= 8 * 1024 * 1024
    for path in files:
        assert path.is_file() and not path.is_symlink(), path
        tracked = subprocess.run(["git", "ls-files", "-s", str(path.relative_to(ROOT))], cwd=ROOT,
                                 capture_output=True, text=True, check=False).stdout.split()
        mode = tracked[0] if tracked else oct(stat.S_IMODE(path.stat().st_mode) | 0o100000)[2:]
        assert mode in {"100644", "100755"}, (path, mode)


@pytest.mark.parametrize("name", ["scripts/sesh_context.py", "scripts/sesh_need.py",
                                  "scripts/drain_queue.py", "skills/search-coding-sessions/SKILL.md"])
def test_sesh_code_is_the_loggers_code_byte_for_byte(name):
    assert (SESH / name).read_bytes() == (LOGGER / name).read_bytes()


def test_vendored_logger_differs_only_on_marked_lines():
    ours = (SESH_SCRIPTS / "session_logging.py").read_text().splitlines()
    theirs = (LOGGER_SCRIPTS / "session_logging.py").read_text().splitlines()
    diff = [line for line in difflib.unified_diff(theirs, ours, lineterm="", n=0)
            if line[:1] in "+-" and not line.startswith(("+++", "---"))]
    added = [line[1:] for line in diff if line.startswith("+")]
    removed = [line[1:] for line in diff if line.startswith("-")]
    assert [line for line in added if line.strip() and "sesh plugin" not in line and not line.startswith("# ")] == []
    assert len(removed) <= 6 and len(added) < 30


# Hooks, run the way Codex runs them ---------------------------------------------------------------

def header(path: Path, **meta) -> str:
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "x", **meta}}) + "\n"
                    + json.dumps({"type": "response_item", "payload": {"text": "PRIVATE TURN"}}) + "\n")
    return str(path)


class Machine:
    """A fake home with the plugins Codex would have installed, and the hooks it would run."""

    def __init__(self, root: Path, *, sesh=True, logger=False, logger_enabled=True, env=None):
        self.root = root
        self.home = root / "home"
        self.codex = self.home / ".codex"
        self.repo = root / "repo"
        self.plugins: list[Path] = []
        self.extra = env or {}
        self.codex.mkdir(parents=True)
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/e3-solutions/sesh.git"],
                       cwd=self.repo, check=True)
        config = []
        if sesh:
            self.plugins.append(self._install(SESH, "jolly-roger-sesh", "sesh", _manifest()["version"]))
            config.append('[plugins."sesh@jolly-roger-sesh"]\nenabled = true\n')
        if logger:
            installed = self._install(LOGGER, "coreedge-local", "codex-session-logging", "0.2.38")
            if logger_enabled:  # Codex runs hooks only for enabled plugins.
                self.plugins.append(installed)
            config.append(f'[plugins."codex-session-logging@coreedge-local"]\nenabled = {str(logger_enabled).lower()}\n')
        (self.codex / "config.toml").write_text("\n".join(config))
        self.transcript = header(root / "rollout.jsonl", thread_source="user", source="vscode")

    def _install(self, source: Path, marketplace: str, name: str, version: str) -> Path:
        target = self.codex / "plugins/cache" / marketplace / name / version
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", ".temp", "supabase"))
        return target

    def env(self) -> dict[str, str]:
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("E3_SESH", "CODEX_SESSION_LOG", "E3_COLLECTIVE", "FORUM_CUE", "CODEX_HOME"))}
        exec_path = subprocess.run(["git", "--exec-path"], capture_output=True, text=True, check=True).stdout.strip()
        env.update({
            "PATH": os.pathsep.join([exec_path, env.get("PATH", "")]),
            "HOME": str(self.home),
            "CODEX_HOME": str(self.codex),
            "CODEX_SESSION_LOG_AUTO_UPLOAD": "0",
            "CODEX_SESSION_LOG_STATE_DIR": str(self.root / "logger-state"),
            "E3_SESH_STATE_DIR": str(self.root / "sesh-state"),
            "E3_COLLECTIVE_FEEDBACK_HOOK_ENABLED": "0",
            "E3_COLLECTIVE_HOOK_ENABLED": "0",
            "FORUM_CUE_ENABLED": "0",
            **self.extra,
        })
        return env

    def fire(self, event: str, **body) -> list[str]:
        """Run every installed plugin's hook command for this event; return their non-empty outputs."""
        payload = {"hook_event_name": event, "session_id": ROUTINE, "turn_id": "turn-1", "cwd": str(self.repo),
                   "transcript_path": self.transcript, **body}
        outputs = []
        for plugin in self.plugins:
            for entry in json.loads((plugin / "hooks/hooks.json").read_text())["hooks"].get(event, []):
                for hook in entry["hooks"]:
                    result = subprocess.run(["sh", "-c", hook["command"]], input=json.dumps(payload), text=True,
                                            capture_output=True, env=self.env(), cwd=self.repo, timeout=60)
                    assert result.returncode == 0, result.stderr
                    if result.stdout.strip():
                        outputs.append(result.stdout.strip())
        return outputs

    def events(self, who: str) -> list[dict]:
        path = self.root / f"{who}-state/events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def queued(self, who: str) -> list[dict]:
        return [json.loads(path.read_text()) for path in sorted((self.root / f"{who}-state").rglob("pending/*.json"))]


def search_response(request_id=REQUEST_ID):
    return {"structuredContent": {"search_request_id": request_id, "results": []}}


def session(machine: Machine, *, session_id=ROUTINE) -> dict:
    """One human turn: startup, a past-work question, two searches, an open and an unrelated tool."""
    out = {}
    out["start"] = machine.fire("SessionStart", source="startup", session_id=session_id)
    out["prompt"] = machine.fire("UserPromptSubmit", prompt=PAST_WORK, session_id=session_id)
    out["first"] = machine.fire("PreToolUse", tool_name=SEARCH, tool_input={"query": "q"}, session_id=session_id,
                                tool_call_id="call-1")
    machine.fire("PostToolUse", tool_name=SEARCH, tool_input={"query": "q"}, tool_response=search_response(),
                 session_id=session_id, tool_call_id="call-1")
    out["second"] = machine.fire("PreToolUse", tool_name=SEARCH, tool_input={"query": "q2"},
                                 session_id=session_id, tool_call_id="call-2")
    machine.fire("PreToolUse", tool_name="shell", tool_input={"cmd": "ls"}, session_id=session_id,
                 tool_call_id="call-3")
    machine.fire("PostToolUse", tool_name="shell", tool_input={"cmd": "ls"}, tool_response={"output": "x"},
                 session_id=session_id, tool_call_id="call-3")
    return out


def denials(outputs: list[str]) -> int:
    return sum(json.loads(item)["hookSpecificOutput"].get("permissionDecision") == "deny" for item in outputs)


def cues(outputs: list[str], text: str) -> int:
    return sum(text in item for item in outputs)


def _sesh_events(machine: Machine, events: list[dict]) -> list[dict]:
    """Sesh events with ids, times, seq and the per-machine paths (and their hash) normalised."""
    events = json.loads(json.dumps(events).replace(str(machine.root), "<root>"))
    keep = []
    for event in events:
        tool = event["metadata"].get("tool_name")
        if event.get("event_type") == "environment_snapshot" or sesh_plugin.is_sesh_tool(tool):
            item = {key: value for key, value in event.items()
                    if key not in {"id", "seq", "created_at", "storage_path", "local_content_path", "thread_id"}}
            item["metadata"] = {key: value for key, value in event["metadata"].items() if key != "codex_setup"}
            keep.append(item)
    return keep


def test_alone_it_runs_every_sesh_hook_with_0_2_38_defaults(tmp_path):
    import sesh_context
    import sesh_need

    machine = Machine(tmp_path)
    out = session(machine)
    assert cues(out["start"], sesh_context.CONTEXT) == 1
    assert cues(out["prompt"], sesh_need.PAST_WORK_CUE) == 1
    assert out["first"] == [] and denials(out["second"]) == 1
    events = machine.events("sesh")
    kinds = [(e["event_type"], e["metadata"].get("tool_name")) for e in events]
    assert kinds == [("environment_snapshot", None), ("tool_call_started", SEARCH), ("tool_call_finished", SEARCH),
                     ("tool_call_started", SEARCH)]
    assert events[1]["metadata"].get("sesh_turn_guard") is None
    assert events[3]["metadata"]["sesh_turn_guard"] == "denied"
    assert events[2]["metadata"]["sesh_request_id"] == REQUEST_ID
    assert not machine.events("logger")


def test_alone_need_only_sessions_get_no_startup_cue(tmp_path):
    import sesh_context

    machine = Machine(tmp_path)
    assert machine.fire("SessionStart", source="startup", session_id=NEED_ONLY) == []
    assert cues(machine.fire("SessionStart", source="startup", session_id=ROUTINE), sesh_context.CONTEXT) == 1


@pytest.mark.parametrize("session_id, env", [
    (ROUTINE, {}),
    (NEED_ONLY, {}),
    (NEED_ONLY, {"E3_SESH_ROUTINE_AB": "0", "E3_SESH_ONE_PER_TURN": "off", "E3_SESH_PAST_WORK_CUE": "no"}),
    (ROUTINE, {"E3_SESH_CONTEXT_ENABLED": "0", "E3_SESH_EXPERIMENT_OPTOUT": "1"}),
    (ROUTINE, {"E3_SESH_CUE_EXPERIMENT": "1", "E3_SESH_CUE_AFTER_COMPACTION": "1"}),
])
def test_alone_matches_codex_session_logging_0_2_38_output_and_sesh_events(tmp_path, session_id, env):
    alone = Machine(tmp_path / "sesh", env=env)
    logger = Machine(tmp_path / "logger", sesh=False, logger=True, env=env)
    assert session(alone, session_id=session_id) == session(logger, session_id=session_id)
    ours = _sesh_events(alone, alone.events("sesh"))
    assert len(ours) == 4 and ours == _sesh_events(logger, logger.events("logger"))


def test_alone_never_stores_prompts_queries_or_other_tools(tmp_path):
    machine = Machine(tmp_path)
    session(machine)
    stored = "".join(path.read_text() for path in (tmp_path / "sesh-state").rglob("*") if path.is_file())
    for secret in (PAST_WORK, '"q2"', "PRIVATE TURN", '"cmd"', "shell"):
        assert secret not in stored
    assert all(e["event_type"] != "message" and "role" not in e for e in machine.events("sesh"))


def test_the_session_snapshot_names_the_sesh_plugin_and_version(tmp_path):
    machine = Machine(tmp_path)
    machine.fire("SessionStart", source="startup")
    [snapshot] = machine.events("sesh")
    detail = json.loads((tmp_path / "sesh-state" / snapshot["local_content_path"]).read_text())
    skills = detail["metadata"]["codex_setup"]["skills"]
    assert {"name": "search-coding-sessions", "plugin": "sesh", "marketplace": "jolly-roger-sesh",
            "version": _manifest()["version"]}.items() <= next(s for s in skills if s.get("plugin") == "sesh").items()


def test_uploads_say_they_come_from_the_sesh_plugin(tmp_path, monkeypatch):
    session_logging = _vendored_logger()

    machine = Machine(tmp_path)
    machine.fire("PostToolUse", tool_name=SEARCH, tool_input={"query": "q"}, tool_response=search_response())
    [record] = machine.queued("sesh")
    monkeypatch.setenv("CODEX_HOME", str(machine.codex))
    payload = session_logging.build_ingest_payload(record, base=tmp_path / "sesh-state")
    assert payload["plugin"] == {"name": "sesh", "version": _manifest()["version"]}
    assert payload["record"]["metadata"]["sesh_request_id"] == REQUEST_ID
    assert payload["client"]["repo_remote"] == "https://github.com/e3-solutions/sesh.git"
    session_logging.validate_ingest_payload(payload)


def test_alone_reuses_the_loggers_installation_id(tmp_path, monkeypatch):
    session_logging = _vendored_logger()

    known = "6f1c2a3b-4d5e-4f60-8a7b-9c0d1e2f3a4b"
    (tmp_path / "codex/session-logging").mkdir(parents=True)
    (tmp_path / "codex/session-logging/installation_id").write_text(known + "\n")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    assert session_logging.local_installation_id(tmp_path / "state") == known
    (tmp_path / "codex/session-logging/installation_id").write_text("not-a-uuid\n")
    fresh = session_logging.local_installation_id(tmp_path / "other")
    assert fresh != known and len(fresh) == 36


def test_linking_switch_stops_events_but_keeps_cues_and_guard(tmp_path):
    import sesh_context

    machine = Machine(tmp_path, env={"E3_SESH_LINKING": "0"})
    out = session(machine)
    assert cues(out["start"], sesh_context.CONTEXT) == 1 and denials(out["second"]) == 1
    assert machine.events("sesh") == []


def test_alongside_codex_session_logging_nothing_fires_twice(tmp_path):
    import sesh_context
    import sesh_need

    machine = Machine(tmp_path, logger=True)
    out = session(machine)
    assert cues(out["start"], sesh_context.CONTEXT) == 1
    assert cues(out["prompt"], sesh_need.PAST_WORK_CUE) == 1
    assert out["first"] == [] and denials(out["second"]) == 1
    assert machine.events("sesh") == []  # The logger links; this plugin posts nothing.
    finished = [e for e in machine.events("logger") if e.get("event_type") == "tool_call_finished"
                and e["metadata"].get("tool_name") == SEARCH]
    assert [e["metadata"]["sesh_request_id"] for e in finished] == [REQUEST_ID]


def test_alongside_the_logger_snapshot_still_records_the_sesh_plugin_version(tmp_path):
    machine = Machine(tmp_path, logger=True)
    machine.fire("SessionStart", source="startup")
    [snapshot] = [e for e in machine.events("logger") if e.get("event_type") == "environment_snapshot"]
    detail = json.loads((tmp_path / "logger-state" / snapshot["local_content_path"]).read_text())
    plugins = {(s.get("plugin"), s.get("version")) for s in detail["metadata"]["codex_setup"]["skills"]}
    assert ("sesh", _manifest()["version"]) in plugins and ("codex-session-logging", "0.2.38") in plugins


@pytest.mark.parametrize("change", ["disabled", "no-cache"])
def test_a_logger_that_cannot_run_leaves_sesh_in_charge(tmp_path, change):
    import sesh_context

    machine = Machine(tmp_path, logger=True, logger_enabled=change != "disabled")
    if change == "no-cache":
        shutil.rmtree(machine.codex / "plugins/cache/coreedge-local")
        machine.plugins = machine.plugins[:1]
    out = session(machine)
    assert cues(out["start"], sesh_context.CONTEXT) == 1 and denials(out["second"]) == 1
    assert len(machine.events("sesh")) == 4


def test_logger_hooks_merged_into_the_global_hooks_file_count_as_active(tmp_path):
    machine = Machine(tmp_path, logger=True, logger_enabled=False)
    (machine.codex / "hooks.json").write_text(
        (LOGGER / "hooks/hooks.json").read_text())
    for event in EVENTS:
        assert sesh_plugin.logger_runs(event, env=machine.env())


@pytest.mark.parametrize("owner, expected", [("sesh", False), ("codex-session-logging", True), ("", True)])
def test_owner_override(tmp_path, owner, expected):
    machine = Machine(tmp_path, logger=True, env={sesh_plugin.OWNER_ENV: owner})
    assert sesh_plugin.logger_runs("PreToolUse", env=machine.env()) is expected


def test_detection_errors_count_as_logger_absent(tmp_path):
    machine = Machine(tmp_path, logger=True)
    (machine.codex / "config.toml").write_bytes(b"\xff\xfe not toml")
    assert sesh_plugin.logger_runs("SessionStart", env=machine.env()) is False


def test_detection_accepts_unparseable_toml_with_plugin_headers(tmp_path):
    machine = Machine(tmp_path, logger=True)
    with (machine.codex / "config.toml").open("a") as handle:
        handle.write("\nbroken = = \n")
    assert sesh_plugin.logger_runs("SessionStart", env=machine.env()) is True


@pytest.mark.parametrize("tool, expected", [
    (SEARCH, True), (OPEN, True), ("mcp__cosmos__sesh__ask_sesh", True),
    ("mcp__codex_apps__cosmos__sesh__open_session_passages", True), ("mcp__cosmos__timetracker__get_chat", True),
    ("shell", False), ("mcp__linear__save_issue", False), (None, False),
])
def test_only_sesh_tools_are_logged(tool, expected):
    assert sesh_plugin.is_sesh_tool(tool) is expected
