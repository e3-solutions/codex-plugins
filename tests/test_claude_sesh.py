"""COR-4888: the Claude Code `sesh` plugin (plugins/claude-sesh).

Same Sesh hooks as the Codex `sesh` plugin, in Claude Code's hook format: the startup cue (with
the routine A/B), the past-work cue, the one-search-per-turn guard, the COR-4887 plan cue and
15% holdout, and metadata-only Sesh search linking. The hook scripts are also run end to end
with fake Claude Code hook payloads.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/claude-sesh"
SCRIPTS = PLUGIN / "scripts"
CODEX_SCRIPTS = ROOT / "plugins/sesh/scripts"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, str(SCRIPTS))
cc = load(SCRIPTS / "sesh_cc.py", "sesh_cc")
sys.modules["sesh_cc"] = cc
linking = load(SCRIPTS / "sesh_linking.py", "sesh_linking")
sys.modules["sesh_linking"] = linking

os.environ.setdefault("E3_SESH_GIT_TIMEOUT_SECONDS", "30")  # Loaded CI machines.
SEARCH = "mcp__cf0ec861-5a4e-415b-b73e-6e017f49ace3__sesh__search_coding_sessions"  # claude.ai connector
LOCAL_SEARCH = "mcp__cosmos__sesh__search_coding_sessions"
ASK = "mcp__cosmos__sesh__ask_sesh"
OPEN = "mcp__cf0ec861-5a4e-415b-b73e-6e017f49ace3__sesh__open_session_passages"
REQUEST_ID = "0b6f7c1e-3d2a-4c5b-9e8f-1a2b3c4d5e6f"
HUMAN = {"CLAUDE_CODE_ENTRYPOINT": "cli"}
PAST_WORK = "Which change hardcoded the routing flag to true?"
SECRET_TEXT = "passage text that must never leave the machine"


def find_session(predicate) -> str:
    for index in range(100000):
        candidate = f"6f0e2a1c-0000-4000-8000-{index:012d}"
        if predicate(candidate):
            return candidate
    raise AssertionError("no session id found")


TREATED_ROUTINE = find_session(lambda s: cc.holdout_bucket(s) >= 15 and cc.routine_arm(s) == "routine")
TREATED_NEED_ONLY = find_session(lambda s: cc.holdout_bucket(s) >= 15 and cc.routine_arm(s) == "need_only")
HOLDOUT = find_session(lambda s: cc.holdout_bucket(s) < 15)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin",
                    "https://github.com/e3-solutions/example.git"], check=True)
    (path / "existing.py").write_text("x = 1\n")
    return path


@pytest.fixture
def other_repo(tmp_path):
    path = tmp_path / "other"
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin",
                    "https://github.com/someone-else/example.git"], check=True)
    return path


@pytest.fixture
def state(tmp_path, monkeypatch):
    base = tmp_path / "state"
    monkeypatch.setenv("E3_SESH_STATE_DIR", str(base))
    monkeypatch.setenv("CLAUDE_SESSION_LOG_AUTO_UPLOAD", "0")
    monkeypatch.setenv("CLAUDE_SESSION_LOG_STATE_DIR", str(tmp_path / "logger-state"))
    for name in list(os.environ):
        if name.startswith("E3_SESH_") and name != "E3_SESH_STATE_DIR":
            monkeypatch.delenv(name)
    monkeypatch.setenv("E3_SESH_GIT_TIMEOUT_SECONDS", "30")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    return base


def payload(event, repo, session=TREATED_ROUTINE, **extra):
    return {"hook_event_name": event, "session_id": session, "cwd": str(repo),
            "transcript_path": "/nonexistent/transcript.jsonl", "permission_mode": "default", **extra}


# Packaging ---------------------------------------------------------------------------------------

def test_versions_agree():
    manifest = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
    market = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    entry = next(p for p in market["plugins"] if p["name"] == "sesh")
    assert manifest["name"] == "sesh"
    assert manifest["version"] == entry["version"] == cc.PLUGIN_VERSION == "0.1.2"
    assert entry["source"] == "./plugins/claude-sesh"


def test_hooks_point_at_real_scripts_and_never_run_a_shell():
    hooks = json.loads((PLUGIN / "hooks/hooks.json").read_text())["hooks"]
    assert set(hooks) == {"SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
                          "PostToolUseFailure"}
    for groups in hooks.values():
        for group in groups:
            for hook in group["hooks"]:
                assert hook["command"] == "python3" and hook["timeout"] <= 10
                script = hook["args"][0].replace("${CLAUDE_PLUGIN_ROOT}/", "")
                assert (PLUGIN / script).is_file()


def test_files_are_plain_and_small():
    files = [p for p in PLUGIN.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    assert len(files) < 50 and sum(p.stat().st_size for p in files) < 1024 * 1024
    for path in PLUGIN.rglob("*"):
        assert not path.is_symlink()
        if path.is_file() and "__pycache__" not in path.parts:
            assert stat.S_IMODE(path.stat().st_mode) & 0o022 == 0


@pytest.mark.parametrize("name, pre, post", [
    (SEARCH, True, True), (LOCAL_SEARCH, True, True), (ASK, True, True), (OPEN, True, True),
    ("mcp__plugin_e3_cosmos__sesh__find_similar_sessions", True, True),
    ("Write", True, False), ("Edit", False, False), ("NotebookEdit", False, False),
    ("mcp__cosmos__forum__search_research_posts", False, False), ("Bash", False, False),
])
def test_matchers(name, pre, post):
    hooks = json.loads((PLUGIN / "hooks/hooks.json").read_text())["hooks"]
    # The matchers use only syntax that means the same in JavaScript and Python regexes.
    assert bool(re.search(hooks["PreToolUse"][0]["matcher"], name)) is pre
    assert bool(re.search(hooks["PostToolUse"][0]["matcher"], name)) is post
    assert hooks["PostToolUseFailure"][0]["matcher"] == hooks["PostToolUse"][0]["matcher"]


# Parity with the Codex plugin --------------------------------------------------------------------

def test_cue_text_and_patterns_match_codex():
    sys.path.insert(0, str(CODEX_SCRIPTS))
    try:
        codex_context = load(CODEX_SCRIPTS / "sesh_context.py", "codex_sesh_context_parity")
        sys.modules.setdefault("sesh_context", codex_context)
        codex_need = load(CODEX_SCRIPTS / "sesh_need.py", "codex_sesh_need_parity")
    finally:
        sys.path.remove(str(CODEX_SCRIPTS))
    assert cc.CONTEXT == codex_context.CONTEXT
    assert cc.COMPACTION_CONTEXT == codex_context.COMPACTION_CONTEXT
    assert cc.PAST_WORK_CUE == codex_need.PAST_WORK_CUE
    assert cc.DENY_REASON.split(" Use ")[0] == codex_need.DENY_REASON.split(" Use ")[0]
    assert cc._PAST_WORK.pattern == codex_need._PAST_WORK.pattern
    assert cc._NOT_A_PERSON_ASKING.pattern == codex_need._NOT_A_PERSON_ASKING.pattern
    for index in range(500):
        sid = f"019a0c52-0000-7000-8000-{index:012d}"
        assert cc.routine_arm(sid) == codex_context.routine_arm(sid)


def test_search_receipt_block_is_the_codex_copy():
    ours = (SCRIPTS / "sesh_linking.py").read_text()
    codex = (CODEX_SCRIPTS / "session_logging.py").read_text()
    start = codex.index("SESH_SEARCH_TOOL_PATTERN = re.compile(")
    end = codex.index("def _direct_source_open_tool")
    block = codex[start:end].rstrip()
    assert block in ours


# Arms --------------------------------------------------------------------------------------------

def test_holdout_is_about_fifteen_percent_and_stable():
    ids = [f"a1b2c3d4-0000-4000-8000-{i:012d}" for i in range(20000)]
    share = sum(cc.holdout_bucket(i) < 15 for i in ids) / len(ids)
    assert 0.14 < share < 0.16
    assert cc.holdout_bucket(HOLDOUT.upper()) == cc.holdout_bucket(f"  {HOLDOUT} ")


def test_holdout_formula_matches_the_documented_sql():
    for sid in (HOLDOUT, TREATED_ROUTINE):
        d = hashlib.sha256(("sesh_holdout_v1:" + sid.strip().lower()).encode()).digest()
        assert cc.holdout_bucket(sid) == (d[0] * 256 + d[1]) % 100


@pytest.mark.parametrize("value, expected", [(None, 15), ("0", 0), ("100", 100), ("x", 15), ("-3", 0), ("250", 100)])
def test_holdout_pct(value, expected):
    assert cc.holdout_pct({} if value is None else {"E3_SESH_HOLDOUT_PCT": value}) == expected


def test_arms():
    p = {"session_id": HOLDOUT}
    assert cc.sesh_arm(p, HUMAN) == "holdout"
    assert cc.sesh_arm(p, {**HUMAN, "E3_SESH_HOLDOUT_PCT": "0"}) == "treated"
    assert cc.sesh_arm({"session_id": TREATED_ROUTINE}, HUMAN) == "treated"
    assert cc.sesh_arm({**p, "agent_id": "a1"}, HUMAN) == "bot"
    assert cc.sesh_arm(p, {"CLAUDE_CODE_ENTRYPOINT": "sdk-ts"}) == "bot"
    assert cc.sesh_arm(p, {**HUMAN, "E3_SESH_EXPERIMENT_OPTOUT": "1"}) == "optout"
    assert cc.sesh_arm({}, HUMAN) == "unassigned"
    meta = cc.arm_metadata({"session_id": TREATED_NEED_ONLY}, HUMAN)
    assert meta == {"sesh_arm": "treated", "sesh_holdout_pct": 15, "sesh_plugin_version": "0.1.2",
                    "sesh_routine_arm": "need_only"}


def test_session_kind(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "user", "entrypoint": "sdk-py"}) + "\n")
    assert cc.session_kind({"transcript_path": str(transcript)}, {}) == "bot"
    transcript.write_text(json.dumps({"type": "user", "entrypoint": "claude-desktop"}) + "\n")
    assert cc.session_kind({"transcript_path": str(transcript)}, {}) == "human"
    assert cc.session_kind({"transcript_path": "/nope"}, {}) == "unknown"
    assert cc.session_kind({"agent_id": "x"}, HUMAN) == "bot"
    assert cc.session_kind({"agent_type": "Explore"}, HUMAN) == "bot"
    assert cc.session_kind({}, {"CLAUDE_CODE_ENTRYPOINT": "claude-vscode"}) == "human"


# SessionStart ------------------------------------------------------------------------------------

def test_startup_cue(repo, other_repo):
    start = lambda session, **kw: cc.startup_cue(payload("SessionStart", repo, session, source="startup", **kw), HUMAN)
    assert start(TREATED_ROUTINE) == cc.CONTEXT
    assert start(TREATED_NEED_ONLY) is None
    assert start(HOLDOUT) is None
    assert cc.startup_cue(payload("SessionStart", repo, TREATED_NEED_ONLY, source="startup"),
                          {**HUMAN, "E3_SESH_ROUTINE_AB": "0"}) == cc.CONTEXT
    # Bots keep the cue (never held out, never in the routine A/B).
    assert cc.startup_cue(payload("SessionStart", repo, HOLDOUT, source="startup"),
                          {"CLAUDE_CODE_ENTRYPOINT": "sdk-ts"}) == cc.CONTEXT
    assert cc.startup_cue(payload("SessionStart", repo, source="clear"), HUMAN) == cc.CONTEXT
    assert cc.startup_cue(payload("SessionStart", repo, source="resume"), HUMAN) is None
    assert cc.startup_cue(payload("SessionStart", repo, source="compact"), HUMAN) is None
    assert cc.startup_cue(payload("SessionStart", repo, source="compact"),
                          {**HUMAN, "E3_SESH_CUE_AFTER_COMPACTION": "1"}) == cc.COMPACTION_CONTEXT
    assert cc.startup_cue(payload("SessionStart", other_repo, source="startup"), HUMAN) is None
    assert cc.startup_cue(payload("SessionStart", repo, source="startup"),
                          {**HUMAN, "E3_SESH_CONTEXT_ENABLED": "0"}) is None


# One search per turn -----------------------------------------------------------------------------

def test_one_search_per_turn(repo, state):
    p = payload("PreToolUse", repo, tool_name=SEARCH, tool_use_id="t1")
    assert cc.one_per_turn_denial(p) is None  # No turn recorded yet: allowed.
    cc.start_turn(payload("UserPromptSubmit", repo))
    assert cc.one_per_turn_denial(p) is None
    denial = json.loads(cc.one_per_turn_denial({**p, "tool_name": LOCAL_SEARCH}))
    assert denial["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert denial["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    for name in (ASK, OPEN):  # Never limited.
        assert cc.one_per_turn_denial({**p, "tool_name": name}) is None
    cc.start_turn(payload("UserPromptSubmit", repo))
    assert cc.one_per_turn_denial(p) is None  # A new turn allows one again.


def test_guard_never_limits_bots_or_opt_outs(repo, state, monkeypatch):
    cc.start_turn(payload("UserPromptSubmit", repo))
    p = payload("PreToolUse", repo, tool_name=SEARCH)
    assert cc.one_per_turn_denial(p) is None
    assert cc.one_per_turn_denial({**p, "agent_id": "sub-1"}) is None
    assert cc.one_per_turn_denial(p, {"CLAUDE_CODE_ENTRYPOINT": "sdk-ts"}) is None
    assert cc.one_per_turn_denial(p, {**HUMAN, "E3_SESH_EXPERIMENT_OPTOUT": "1"}) is None
    assert cc.one_per_turn_denial(p, {**HUMAN, "E3_SESH_ONE_PER_TURN": "0"}) is None
    assert cc.one_per_turn_denial(p) is not None


def test_guard_fails_open(repo, state, monkeypatch):
    cc.start_turn(payload("UserPromptSubmit", repo))
    monkeypatch.setattr(cc, "_search_marker", lambda *a: (_ for _ in ()).throw(OSError("disk")))
    assert cc.one_per_turn_denial(payload("PreToolUse", repo, tool_name=SEARCH)) is None


# Past-work cue -----------------------------------------------------------------------------------

def test_past_work_cue(repo, other_repo, state):
    p = payload("UserPromptSubmit", repo, prompt=PAST_WORK)
    assert cc.past_work_cue(p) == cc.PAST_WORK_CUE
    assert cc.past_work_cue({**p, "prompt": "add a retry to the uploader"}) is None
    assert cc.past_work_cue({**p, "session_id": HOLDOUT}) is None
    assert cc.past_work_cue(p, {"CLAUDE_CODE_ENTRYPOINT": "sdk-ts"}) is None
    assert cc.past_work_cue(payload("UserPromptSubmit", other_repo, prompt=PAST_WORK)) is None
    assert cc.past_work_cue(p, {**HUMAN, "E3_SESH_PAST_WORK_CUE": "off"}) is None


# Plan cue ----------------------------------------------------------------------------------------

def write(repo, relative, session=TREATED_ROUTINE):
    return payload("PreToolUse", repo, session, tool_name="Write",
                   tool_input={"file_path": str(repo / relative), "content": SECRET_TEXT})


@pytest.mark.parametrize("relative, kind", [
    ("services/billing/worker.py", "service"),
    ("db/migrations/0042_add_orders.sql", "migration or table"),
    ("schema.sql", "migration or table"),
    ("scripts/backfill_orders.py", "script"),
    ("src/pricing/quote.ts", "module"),
    ("tests/test_quote.py", None), ("src/quote.test.ts", None), ("README.md", None),
    ("config/app.yaml", None), ("src/pricing/__init__.py", None), (".github/workflows/x.py", None),
    ("node_modules/pkg/index.js", None),
])
def test_plan_kind(relative, kind):
    assert cc.plan_kind(relative) == kind


def test_plan_cue_fires_once_per_area_and_twice_per_session(repo, state):
    cue, meta = cc.plan_cue_decision(write(repo, "services/billing/worker.py"))
    assert "existing-work card" in cue and "services/billing/worker.py" in cue
    assert meta["sesh_plan_cue"] == "shown" and meta["sesh_plan_kind"] == "service"
    assert SECRET_TEXT not in json.dumps(meta) and "billing" not in json.dumps(meta)
    assert cc.plan_cue_decision(write(repo, "services/billing/models.py")) == (None, None)
    cue, meta = cc.plan_cue_decision(write(repo, "scripts/backfill.py"))
    assert cue and meta["sesh_plan_cue_index"] == 2
    assert cc.plan_cue_decision(write(repo, "src/other/thing.py")) == (None, None)  # Max 2.


def test_plan_cue_skips(repo, other_repo, state, tmp_path):
    assert cc.plan_cue_decision(write(repo, "existing.py")) == (None, None)  # Not a new file.
    assert cc.plan_cue_decision({**write(repo, "src/a.py"), "agent_id": "s"}) == (None, None)
    outside = write(repo, "x.py")
    outside["tool_input"] = {"file_path": str(tmp_path / "scratch.py")}
    assert cc.plan_cue_decision(outside) == (None, None)
    assert cc.plan_cue_decision(write(other_repo, "src/a.py")) == (None, None)
    assert cc.plan_cue_decision(write(repo, "src/a.py"), {**HUMAN, "E3_SESH_PLAN_CUE": "0"}) == (None, None)
    edit = {**write(repo, "src/a.py"), "tool_name": "Edit"}
    assert cc.plan_cue_decision(edit) == (None, None)


def test_plan_cue_respects_the_search_guard(repo, state):
    cc.start_turn(payload("UserPromptSubmit", repo))
    assert cc.one_per_turn_denial(payload("PreToolUse", repo, tool_name=SEARCH)) is None
    assert cc.plan_cue_decision(write(repo, "src/pricing/quote.ts")) == (None, None)


def test_plan_cue_holdout_logs_but_shows_nothing(repo, state):
    cue, meta = cc.plan_cue_decision(write(repo, "src/pricing/quote.ts", session=HOLDOUT))
    assert cue is None and meta["sesh_plan_cue"] == "withheld"


# Linking -----------------------------------------------------------------------------------------

def search_body():
    return json.dumps({"results": [], "search_request_id": REQUEST_ID, "note": SECRET_TEXT})


@pytest.mark.parametrize("response", [
    search_body(),
    [{"type": "text", "text": search_body()}],
    {"content": [{"type": "text", "text": search_body()}]},
])
def test_request_id_from_every_claude_response_shape(response):
    meta = linking.search_metadata({"tool_name": SEARCH, "tool_response": response})
    assert meta["sesh_request_id"] == REQUEST_ID and meta["sesh_request_ids"] == [REQUEST_ID]
    assert SECRET_TEXT not in json.dumps(meta)


def test_no_request_id_for_other_tools_or_bad_bodies():
    assert linking.search_metadata({"tool_name": ASK, "tool_response": search_body()}) == {}
    assert linking.search_metadata({"tool_name": SEARCH, "tool_response": "not json"}) == {}


def test_capture_and_upload_payload(repo, other_repo, state):
    p = payload("PostToolUse", repo, tool_name=SEARCH, tool_use_id="toolu_1",
                tool_input={"query": SECRET_TEXT}, tool_response=search_body())
    meta = linking.tool_metadata(p, "finished")
    meta.update(linking.search_metadata(p))
    record = linking.capture(p, event_type="tool_call_finished", hook_event="PostToolUse", metadata=meta)
    assert record["seq"] > linking.SEQ_OFFSET
    sent = []
    result = linking.drain_queue(post=sent.append)
    assert result["uploaded"] == 1 and result["remaining"] == 0
    body = sent[0]
    assert body["plugin"] == {"name": "sesh", "version": "0.1.2"}
    assert body["client"]["repo_remote"] == "https://github.com/e3-solutions/example.git"
    metadata = body["event"]["metadata"]
    assert metadata["platform"] == "claude-code" and metadata["agent"] == "claude"
    assert metadata["sesh_request_id"] == REQUEST_ID and metadata["tool_phase"] == "finished"
    assert metadata["sesh_arm"] == "treated" and metadata["sesh_plugin_version"] == "0.1.2"
    assert SECRET_TEXT not in json.dumps(body)
    # The ingest keeps sesh_request_id only for this exact shape (event_sanitizer.ts).
    assert re.fullmatch(r"(?:[A-Za-z0-9_-]+__)?(?:sesh__)?search_coding_sessions", metadata["tool_name"])
    # Gates: other organisations and the switch.
    assert linking.capture(payload("PostToolUse", other_repo, tool_name=SEARCH),
                           event_type="tool_call_finished", hook_event="PostToolUse") is None


def test_linking_switch(repo, state, monkeypatch):
    monkeypatch.setenv("E3_SESH_LINKING", "0")
    assert linking.capture(payload("SessionStart", repo), event_type="environment_snapshot",
                           hook_event="SessionStart") is None


def test_environment_snapshot_names_the_plugin(monkeypatch):
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/h/.claude/plugins/cache/coreedge-internal/sesh/0.1.0")
    setup = linking.environment_metadata({})["codex_setup"]
    assert setup["skills"] == [{"name": "search-coding-sessions", "source": "plugin", "plugin": "sesh",
                                "marketplace": "coreedge-internal", "version": "0.1.2"}]
    assert setup["plugins"] == [{"name": "sesh@coreedge-internal", "enabled": True}]


def test_duration(state):
    p = {"session_id": TREATED_ROUTINE, "tool_use_id": "toolu_9"}
    linking.mark_started(p)
    assert isinstance(linking.duration_ms(p), int)
    assert linking.duration_ms({**p, "duration_ms": 812}) == 812


# The hook scripts, end to end with fake Claude Code payloads -------------------------------------

def run_hook(script, body, env):
    result = subprocess.run([sys.executable, str(SCRIPTS / script)], input=json.dumps(body),
                            capture_output=True, text=True, env=env, timeout=120)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def queued(base):
    folder = base / "session-logging/queue/pending"
    return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))] if folder.exists() else []


def test_hook_scripts_end_to_end(repo, tmp_path):
    base = tmp_path / "hookstate"
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path / "home"), "E3_SESH_STATE_DIR": str(base),
           "CLAUDE_SESSION_LOG_AUTO_UPLOAD": "0", "CLAUDE_CODE_ENTRYPOINT": "cli", "E3_SESH_GIT_TIMEOUT_SECONDS": "30",
           "CLAUDE_PLUGIN_ROOT": str(tmp_path / "plugins/cache/coreedge-internal/sesh/0.1.0")}
    out = run_hook("session_start.py", payload("SessionStart", repo, source="startup"), env)
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"] == cc.CONTEXT
    out = run_hook("user_prompt_submit.py", payload("UserPromptSubmit", repo, prompt=PAST_WORK), env)
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"] == cc.PAST_WORK_CUE
    first = payload("PreToolUse", repo, tool_name=SEARCH, tool_use_id="toolu_a", tool_input={"query": SECRET_TEXT})
    assert run_hook("pre_tool_use.py", first, env) == ""
    second = {**first, "tool_use_id": "toolu_b"}
    denied = json.loads(run_hook("pre_tool_use.py", second, env))
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert run_hook("pre_tool_use.py", {**second, "tool_name": ASK}, env) == ""
    finished = payload("PostToolUse", repo, tool_name=SEARCH, tool_use_id="toolu_a",
                       tool_input={"query": SECRET_TEXT}, tool_response=search_body())
    assert run_hook("post_tool_use.py", finished, env) == ""
    failed = payload("PostToolUseFailure", repo, tool_name=ASK, tool_use_id="toolu_c", error=SECRET_TEXT)
    assert run_hook("post_tool_use.py", failed, env) == ""
    # Plan cue: blocked by this turn's search; next turn it fires.
    assert run_hook("pre_tool_use.py", write(repo, "services/orders/api.py"), env) == ""
    run_hook("user_prompt_submit.py", payload("UserPromptSubmit", repo, prompt="build the orders api"), env)
    cue = json.loads(run_hook("pre_tool_use.py", write(repo, "services/orders/api.py"), env))
    assert cue["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in cue["hookSpecificOutput"]
    assert run_hook("pre_tool_use.py", payload("PreToolUse", repo, tool_name="Bash"), env) == ""
    assert run_hook("pre_tool_use.py", {}, env) == ""  # Garbage in, nothing out, exit 0.

    events = queued(base)
    types = sorted(e["event_type"] for e in events)
    assert types == ["environment_snapshot", "sesh_plan_cue_shown", "tool_call_failed",
                     "tool_call_finished", "tool_call_started", "tool_call_started", "tool_call_started"]
    by_type = {e["event_type"]: e for e in events}
    assert by_type["tool_call_finished"]["metadata"]["sesh_request_id"] == REQUEST_ID
    assert isinstance(by_type["tool_call_finished"]["metadata"]["duration_ms"], int)
    assert by_type["tool_call_failed"]["metadata"]["success"] is False
    assert any(e["metadata"].get("sesh_turn_guard") == "denied" for e in events)
    assert by_type["environment_snapshot"]["metadata"]["codex_setup"]["skills"][0]["marketplace"] == "coreedge-internal"
    dump = json.dumps(events) + "".join(p.read_text() for p in base.rglob("*") if p.is_file())
    assert SECRET_TEXT not in dump and PAST_WORK not in dump and "orders" not in dump


def test_hook_scripts_for_bots_and_holdout(repo, tmp_path):
    base = tmp_path / "botstate"
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path / "home"), "E3_SESH_STATE_DIR": str(base),
           "CLAUDE_SESSION_LOG_AUTO_UPLOAD": "0", "CLAUDE_CODE_ENTRYPOINT": "sdk-ts",
           "E3_SESH_GIT_TIMEOUT_SECONDS": "30"}
    run_hook("user_prompt_submit.py", payload("UserPromptSubmit", repo, prompt="go"), env)
    for call in ("a", "b", "c"):
        assert run_hook("pre_tool_use.py", payload("PreToolUse", repo, tool_name=SEARCH, tool_use_id=call), env) == ""
    held = {**env, "CLAUDE_CODE_ENTRYPOINT": "cli"}
    assert run_hook("session_start.py", payload("SessionStart", repo, HOLDOUT, source="startup"), held) == ""
    assert run_hook("user_prompt_submit.py", payload("UserPromptSubmit", repo, HOLDOUT, prompt=PAST_WORK), held) == ""
    assert run_hook("pre_tool_use.py", write(repo, "src/new_module.py", session=HOLDOUT), held) == ""
    arms = {e["metadata"]["sesh_arm"] for e in queued(base)}
    assert arms == {"bot", "holdout"}


# Installer (a fake `claude` CLI; the real one is exercised by hand in a throwaway CLAUDE_CONFIG_DIR) ---

FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
path = os.environ["FAKE_CLAUDE_STATE"]
state = json.load(open(path))
args = [a for a in sys.argv[1:] if a != "--json"]
state.setdefault("calls", []).append(args)
out = None
if args[:2] == ["plugin", "list"]:
    out = [dict(p, id=k) for k, p in state["plugins"].items()]
elif args[:3] == ["plugin", "marketplace", "list"]:
    out = [dict(m, name=k) for k, m in state["markets"].items()]
elif args[:3] == ["plugin", "marketplace", "add"]:
    state["markets"]["coreedge-internal"] = {"source": "git", "url": args[3]}
elif args[:3] == ["plugin", "marketplace", "update"]:
    pass
elif args[:2] == ["plugin", "install"]:
    state["plugins"][args[2]] = {"version": "0.1.0", "scope": "user", "enabled": True}
elif args[:2] == ["plugin", "enable"]:
    state["plugins"][args[2]]["enabled"] = True
elif args[:2] == ["plugin", "uninstall"]:
    state["plugins"].pop(args[2])
json.dump(state, open(path, "w"))
if out is not None:
    print(json.dumps(out))
'''


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    binary = tmp_path / "claude"
    binary.write_text(FAKE_CLAUDE)
    binary.chmod(0o755)
    state_path = tmp_path / "fake_state.json"
    monkeypatch.setenv("FAKE_CLAUDE_STATE", str(state_path))

    def setup(plugins=None, markets=None):
        state_path.write_text(json.dumps({"plugins": plugins or {}, "markets": markets or {}}))
        return str(binary)

    setup.read = lambda: json.loads(state_path.read_text())
    return setup


def test_installer(fake_claude):
    installer = load(PLUGIN / "install.py", "claude_sesh_install")
    other = {"claude-session-logging@coreedge-internal": {"version": "0.2.18", "scope": "user", "enabled": True}}
    claude = fake_claude(plugins=dict(other))
    result = installer.install(claude)
    assert result["status"] == "installed" and result["plugin"] == "sesh@coreedge-internal"
    state = fake_claude.read()
    assert state["markets"]["coreedge-internal"]["url"] == installer.SOURCES[0]
    assert state["plugins"]["claude-session-logging@coreedge-internal"] == other["claude-session-logging@coreedge-internal"]
    assert installer.install(claude)["status"] == "unchanged"
    state["plugins"]["sesh@coreedge-internal"]["enabled"] = False
    fake_claude(plugins=state["plugins"], markets=state["markets"])
    repaired = installer.install(claude)
    assert repaired["status"] == "repaired" and "enabled" in repaired["actions"]
    assert installer.uninstall(claude)["status"] == "uninstalled"
    state = fake_claude.read()
    assert "coreedge-internal" in state["markets"] and list(state["plugins"]) == list(other)
    assert installer.uninstall(claude)["status"] == "not-installed"


def test_installer_refuses_a_second_sesh(fake_claude):
    installer = load(PLUGIN / "install.py", "claude_sesh_install_conflict")
    claude = fake_claude(plugins={"sesh@somewhere-else": {"version": "9", "enabled": True}})
    assert installer.install(claude)["status"] == "skipped"
    assert not any(call[:2] == ["plugin", "install"] for call in fake_claude.read()["calls"])


def test_failed_search_gives_the_turn_its_search_back(repo, state):
    cc.start_turn(payload("UserPromptSubmit", repo))
    p = payload("PreToolUse", repo, tool_name=SEARCH)
    assert cc.one_per_turn_denial(p) is None
    cc.release_search(p)
    assert cc.one_per_turn_denial(p) is None
    assert cc.one_per_turn_denial(p) is not None


def test_shares_the_logger_installation_id_and_drops_stale_events(repo, state, tmp_path):
    logger = tmp_path / "logger-state"
    logger.mkdir(exist_ok=True)
    (logger / "installation_id").write_text("logger-id\n")
    linking.capture(payload("SessionStart", repo), event_type="environment_snapshot", hook_event="SessionStart")
    sent = []
    linking.drain_queue(post=sent.append)
    assert sent[0]["client"]["installation_id"] == "logger-id"
    linking.capture(payload("SessionStart", repo), event_type="environment_snapshot", hook_event="SessionStart")
    for path in (state / "session-logging/queue/pending").glob("*.json"):
        os.utime(path, (0, 0))
    result = linking.drain_queue(post=sent.append)
    assert result["uploaded"] == 0 and result["dead_lettered"] == 1 and len(sent) == 1
