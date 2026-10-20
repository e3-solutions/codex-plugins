"""COR-4887: the existing-work plan cue, the 15% session holdout, guard-denial logging and exposure
events, in both Codex copies (codex-session-logging and plugins/sesh), plus parity with the Claude
Code plugin (plugins/claude-sesh, COR-4888) so a session id lands in the same arm in every tool."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COPIES = {
    "logger": ROOT / "plugins/codex-session-logging/scripts",
    "sesh": ROOT / "plugins/sesh/scripts",
}
CLAUDE = ROOT / "plugins/claude-sesh/scripts"
SHARED = ("sesh_context.py", "sesh_need.py", "plan_cue.py", "sesh_hooks.py")


def _load(folder: Path, name: str, alias: str):
    """Import one copy's module under its own name, resolving its sibling imports from that folder."""
    saved = {key: sys.modules.pop(key) for key in list(sys.modules)
             if key in {"sesh_context", "sesh_need", "plan_cue", "sesh_hooks", "session_logging"}}
    sys.path.insert(0, str(folder))
    try:
        spec = importlib.util.spec_from_file_location(alias, folder / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(folder))
        for key in [k for k in list(sys.modules) if k in {"sesh_context", "sesh_need", "plan_cue",
                                                          "sesh_hooks", "session_logging"}]:
            sys.modules.pop(key)
        sys.modules.update(saved)


ctx = _load(COPIES["logger"], "sesh_context", "cor4887_ctx")
cc = _load(CLAUDE, "sesh_cc", "cor4887_sesh_cc")


def ids(n=400):
    return [f"019a0c52-0000-7000-8000-{index:012d}" for index in range(n)]


def first(pred):
    return next(sid for sid in ids(2000) if pred(sid))


TREATED = first(lambda s: ctx.holdout_bucket(s) >= 50)
HOLDOUT = first(lambda s: ctx.holdout_bucket(s) < 15)


# --- copies and cross-plugin parity ---------------------------------------------------------------

@pytest.mark.parametrize("name", SHARED)
def test_both_codex_copies_are_byte_identical(name):
    assert (COPIES["logger"] / name).read_bytes() == (COPIES["sesh"] / name).read_bytes()


def test_holdout_hash_is_the_documented_formula_and_matches_claude_code():
    for sid in ids() + ["  ABC-def ", "x"]:
        raw = hashlib.sha256(f"sesh_holdout_v1:{sid.strip().lower()}".encode()).digest()
        assert ctx.holdout_bucket(sid) == (raw[0] * 256 + raw[1]) % 100 == cc.holdout_bucket(sid)
        assert ctx.routine_arm(sid) == cc.routine_arm(sid)


@pytest.mark.parametrize("raw", [None, "", "0", "15", "30", "100", "250", "-3", "off", "abc"])
def test_holdout_pct_parses_like_claude_code(raw):
    env = {} if raw is None else {"E3_SESH_HOLDOUT_PCT": raw}
    assert ctx.holdout_pct(env) == cc.holdout_pct(env)


def test_same_session_lands_in_the_same_arm_in_codex_and_claude_code(tmp_path):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(json.dumps({"type": "session_meta", "payload": {"thread_source": "user"}}) + "\n")
    held = 0
    for sid in ids():
        codex = ctx.sesh_arm({"session_id": sid, "transcript_path": str(transcript)}, {})
        claude = cc.sesh_arm({"session_id": sid}, {"CLAUDE_CODE_ENTRYPOINT": "cli"})
        assert codex == claude, sid
        held += codex == "holdout"
    assert 40 <= held <= 80  # About 15% of 400.


def test_plan_rules_and_text_match_claude_code():
    plan = _load(COPIES["logger"], "plan_cue", "cor4887_plan")
    assert plan.PLAN_CUE == cc.PLAN_CUE and plan.PLAN_CUE_MAX_PER_SESSION == cc.PLAN_CUE_MAX_PER_SESSION == 2
    assert plan.CODE_SUFFIXES == cc.CODE_SUFFIXES and plan.TRIVIAL_NAMES == cc.TRIVIAL_NAMES
    for pattern in ("_TEST_PATH", "_SKIP_PATH", "_MIGRATION_PATH", "_SERVICE_PATH", "_SCRIPT_PATH",
                    "PLAN_SKIP_PATH"):
        assert getattr(plan, pattern).pattern == getattr(cc, pattern).pattern, pattern
    for path in ["svc/billing/invoice_sync.py", "supabase/migrations/001_gate.sql", "scripts/backfill.sh",
                 "Dockerfile", "src/api/routes.ts", "tests/test_x.py", "reviews/cor-4581/main.tsx",
                 "outputs/2026-10-19/run.py", "app/__init__.py", "lib/x.md", "a/b/c/d.go"]:
        assert plan.plan_kind(path) == cc.plan_kind(path), path
        assert plan.plan_topic(path) == cc.plan_topic(path), path
    assert plan.digest(" AbC ") == cc.digest("abc")
    assert _load(COPIES["logger"], "sesh_need", "cor4887_need").DENY_REASON == cc.DENY_REASON


# --- plan cue, run in-process against a real e3 repository ----------------------------------------

class Repo:
    def __init__(self, tmp: Path, *, thread_source="user", origin="https://github.com/e3-solutions/sesh.git"):
        self.root = tmp / "repo"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "remote", "add", "origin", origin], cwd=self.root, check=True)
        self.transcript = tmp / "rollout.jsonl"
        self.transcript.write_text(json.dumps({"type": "session_meta",
                                               "payload": {"thread_source": thread_source}}) + "\n")
        self.state = tmp / "state"
        self.home = tmp / "codex"

    def patch(self, *lines, sid=TREATED, turn="t1", path_prefix=""):
        body = "*** Begin Patch\n" + "".join(f"{line}\n+x\n" for line in lines) + "*** End Patch"
        return {"hook_event_name": "PreToolUse", "session_id": sid, "turn_id": turn, "cwd": str(self.root),
                "transcript_path": str(self.transcript), "tool_name": "apply_patch",
                "tool_input": {"command": body}}


@pytest.fixture(params=sorted(COPIES))
def plan(request, monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    module = _load(COPIES[request.param], "plan_cue", f"cor4887_plan_{request.param}")
    # pytest's tmp_path is under /var/folders, which the cue treats as scratch; allow it here only.
    module._SCRATCH = __import__("re").compile(r"^/(?:private/)?tmp/scratch/")
    return module


def decide(plan, repo, payload, env=None):
    return plan.decide(payload, env or {}, directory=repo.state)


def test_new_module_cues_once_with_the_card_instruction(plan, tmp_path):
    repo = Repo(tmp_path)
    cue, meta = decide(plan, repo, repo.patch("*** Add File: svc/billing/invoice_sync.py"))
    assert "existing-work card" in cue and "reuse or extend it" in cue and "svc/billing/invoice_sync.py" in cue
    assert meta["sesh_plan_cue"] == "shown" and meta["sesh_plan_kind"] == "module"
    assert meta["sesh_plan_cue_index"] == 1 and len(meta["sesh_plan_topic_sha256"]) == 64
    # Same area again (a different folder inside svc/billing): no second cue.
    assert decide(plan, repo, repo.patch("*** Add File: svc/billing/tax/rates.py")) == (None, None)


def test_never_on_tests_scratch_reviews_or_non_code(plan, tmp_path):
    repo = Repo(tmp_path)
    for line in ["*** Add File: tests/test_billing.py", "*** Add File: reviews/cor-4581/main.tsx",
                 "*** Add File: outputs/2026-10-19/analysis.py", "*** Add File: notes/plan.md",
                 "*** Add File: /tmp/scratch/run.py", "*** Add File: pkg/__init__.py",
                 "*** Update File: svc/billing/invoice.py"]:
        assert decide(plan, repo, repo.patch(line)) == (None, None), line


def test_continuation_of_a_folder_already_changed_never_cues(plan, tmp_path):
    repo = Repo(tmp_path)
    assert decide(plan, repo, repo.patch("*** Update File: svc/billing/invoice.py")) == (None, None)
    assert decide(plan, repo, repo.patch("*** Add File: svc/billing/helpers2.py")) == (None, None)
    cue, _ = decide(plan, repo, repo.patch("*** Add File: workers/digest/run_digest.py"))
    assert cue is not None


def test_at_most_two_per_session(plan, tmp_path):
    repo = Repo(tmp_path)
    shown = [decide(plan, repo, repo.patch(f"*** Add File: area{i}/mod{i}/thing.py", turn=f"t{i}"))[1]
             for i in range(4)]
    assert [m and m["sesh_plan_cue_index"] for m in shown] == [1, 2, None, None]


def test_never_in_a_turn_that_already_searched(plan, tmp_path, monkeypatch):
    repo = Repo(tmp_path)
    marker = tmp_path / "codex" / "sesh-turn-guard" / plan.digest(TREATED) / plan.digest("t1")
    marker.parent.mkdir(parents=True)
    marker.write_text("")
    assert decide(plan, repo, repo.patch("*** Add File: svc/billing/invoice_sync.py")) == (None, None)
    assert decide(plan, repo, repo.patch("*** Add File: svc/other/x.py", turn="t2"))[0] is not None


def test_holdout_logs_withheld_and_shows_nothing(plan, tmp_path):
    repo = Repo(tmp_path)
    cue, meta = decide(plan, repo, repo.patch("*** Add File: svc/billing/invoice_sync.py", sid=HOLDOUT))
    assert cue is None and meta["sesh_plan_cue"] == "withheld"
    off = decide(plan, repo, repo.patch("*** Add File: svc/x/y.py", sid=HOLDOUT), {"E3_SESH_HOLDOUT_PCT": "0"})
    assert off[0] is not None


@pytest.mark.parametrize("env", [{"E3_SESH_PLAN_CUE": "0"}, {"E3_SESH_EXPERIMENT_OPTOUT": "1"},
                                 {"E3_SESH_CONTEXT_ENABLED": "0"}])
def test_switches_turn_it_off(plan, tmp_path, env):
    repo = Repo(tmp_path)
    assert decide(plan, repo, repo.patch("*** Add File: svc/billing/invoice_sync.py"), env) == (None, None)


def test_bots_and_non_e3_repositories_get_no_plan_cue(plan, tmp_path):
    bot = Repo(tmp_path / "a" if (tmp_path / "a").mkdir() is None else tmp_path, thread_source="subagent")
    assert decide(plan, bot, bot.patch("*** Add File: svc/billing/invoice_sync.py")) == (None, None)
    (tmp_path / "b").mkdir()
    other = Repo(tmp_path / "b", origin="https://github.com/someone/else.git")
    assert decide(plan, other, other.patch("*** Add File: svc/billing/invoice_sync.py")) == (None, None)


def test_plan_cue_wrapper_fails_open(plan, monkeypatch):
    monkeypatch.setattr(plan, "decide", lambda *a, **k: 1 / 0)
    assert plan.plan_cue({"tool_name": "apply_patch"}) == (None, None)


# --- holdout removes every nudge, the guard, and logs exposure -------------------------------------

def _header(tmp_path, source="user"):
    path = tmp_path / f"h-{source}.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {"thread_source": source}}) + "\n")
    return str(path)


def test_startup_cue_withheld_in_holdout_and_logged_as_exposure(tmp_path, monkeypatch):
    monkeypatch.setattr(ctx, "_e3_repository", lambda payload: True)
    base = {"hook_event_name": "SessionStart", "source": "startup", "transcript_path": _header(tmp_path)}
    cue, _, nudge = ctx.startup_nudge({**base, "session_id": HOLDOUT}, {})
    assert cue is None and nudge == {"sesh_startup_cue": "withheld", "sesh_withheld_by": "holdout"}
    treated = first(lambda s: ctx.holdout_bucket(s) >= 15 and ctx.routine_arm(s) == "routine")
    cue, _, nudge = ctx.startup_nudge({**base, "session_id": treated}, {})
    assert cue == ctx.CONTEXT and nudge == {"sesh_startup_cue": "shown"}
    bot = {**base, "session_id": HOLDOUT, "transcript_path": _header(tmp_path, "subagent")}
    assert ctx.startup_nudge(bot, {})[0] == ctx.CONTEXT  # Bots are never held out.


def test_arm_labels_compose_holdout_first_then_routine_ab(tmp_path):
    header = _header(tmp_path)
    assert ctx.arm_metadata({"session_id": HOLDOUT, "transcript_path": header}, {}) == {
        "sesh_arm": "holdout", "sesh_holdout_pct": 15}
    meta = ctx.arm_metadata({"session_id": TREATED, "transcript_path": header}, {})
    assert meta["sesh_arm"] == "treated" and meta["sesh_routine_arm"] in {"routine", "need_only"}
    assert "sesh_routine_arm" not in ctx.arm_metadata({"session_id": TREATED, "transcript_path": header},
                                                      {"E3_SESH_ROUTINE_AB": "0"})
    # Within the treated sessions the routine A/B stays about 50/50.
    treated = [s for s in ids(2000) if ctx.holdout_bucket(s) >= 15]
    share = sum(ctx.routine_arm(s) == "need_only" for s in treated) / len(treated)
    assert 0.45 < share < 0.55


@pytest.mark.parametrize("copy", sorted(COPIES))
def test_guard_and_past_work_cue_are_off_in_holdout(copy, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    need = _load(COPIES[copy], "sesh_need", f"cor4887_need_{copy}")
    search = {"hook_event_name": "PreToolUse", "tool_name": "mcp__cosmos__sesh__search_coding_sessions",
              "turn_id": "t1", "transcript_path": _header(tmp_path)}
    for _ in range(2):
        assert need.one_per_turn_denial({**search, "session_id": HOLDOUT}, {}) is None
    assert need.one_per_turn_denial({**search, "session_id": TREATED}, {}) is None
    assert need.one_per_turn_denial({**search, "session_id": TREATED}, {}) is not None
    monkeypatch.setattr(sys.modules["sesh_need"] if "sesh_need" in sys.modules else need, "_e3_repository",
                        lambda payload: True, raising=False)
    need._e3_repository = lambda payload: True
    prompt = {"hook_event_name": "UserPromptSubmit", "prompt": "Which change hardcoded the routing flag?",
              "transcript_path": _header(tmp_path)}
    assert need.past_work_decision({**prompt, "session_id": HOLDOUT}, {}) == (
        None, {"sesh_past_work_cue": "withheld", "sesh_withheld_by": "holdout"})
    assert need.past_work_decision({**prompt, "session_id": TREATED}, {}) == (
        need.PAST_WORK_CUE, {"sesh_past_work_cue": "shown"})


@pytest.mark.parametrize("copy", sorted(COPIES))
def test_denials_carry_the_first_searchs_outcome_and_neutral_text(copy, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    need = _load(COPIES[copy], "sesh_need", f"cor4887_need2_{copy}")
    assert "has not helped" not in need.DENY_REASON and "E3_SESH_ONE_PER_TURN" in need.DENY_REASON
    search = {"hook_event_name": "PreToolUse", "tool_name": "mcp__cosmos__sesh__search_coding_sessions",
              "session_id": TREATED, "turn_id": "t1", "transcript_path": _header(tmp_path)}
    assert need.one_per_turn_denial(search, {}) is None
    assert need.one_per_turn_denial(search, {}) is not None
    assert need.denied_metadata() == {"sesh_turn_guard": "denied", "sesh_turn_guard_first": "unknown"}
    need.record_search_outcome(search, False)
    assert need.one_per_turn_denial(search, {}) is not None
    assert need.denied_metadata()["sesh_turn_guard_first"] == "failed"
    need.record_search_outcome(search, True)  # The first outcome is kept, not overwritten.
    need.one_per_turn_denial(search, {})
    assert need.denied_metadata()["sesh_turn_guard_first"] == "failed"
    other = {**search, "turn_id": "t2"}
    need.one_per_turn_denial(other, {})
    need.record_search_outcome(other, True)
    need.one_per_turn_denial(other, {})
    assert need.denied_metadata()["sesh_turn_guard_first"] == "succeeded"


def test_exposure_event_types():
    hooks = _load(COPIES["logger"], "sesh_hooks", "cor4887_hooks")
    assert hooks.nudge_event_type({"sesh_plan_cue": "shown"}) == "sesh_plan_cue_shown"
    assert hooks.nudge_event_type({"sesh_plan_cue": "withheld"}) == "sesh_plan_cue_withheld"
    assert hooks.nudge_event_type({"sesh_startup_cue": "withheld", "sesh_withheld_by": "holdout"}) == \
        "sesh_startup_cue_withheld"
    assert hooks.nudge_event_type({"sesh_past_work_cue": "shown"}) == "sesh_past_work_cue_shown"
    assert hooks.nudge_event_type({}) is None


@pytest.mark.parametrize("copy", sorted(COPIES))
def test_holdout_keeps_the_turn_marker_so_the_plan_cue_skips_searched_turns(copy, tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    need = _load(COPIES[copy], "sesh_need", f"cor4887_need3_{copy}")
    plan = _load(COPIES[copy], "plan_cue", f"cor4887_plan3_{copy}")
    plan._SCRATCH = __import__("re").compile(r"^/(?:private/)?tmp/scratch/")
    repo = Repo(tmp_path)
    search = {"hook_event_name": "PreToolUse", "tool_name": "mcp__cosmos__sesh__search_coding_sessions",
              "session_id": HOLDOUT, "turn_id": "t1", "transcript_path": str(repo.transcript)}
    assert need.one_per_turn_denial(search, {}) is None and need.one_per_turn_denial(search, {}) is None
    assert plan.decide(repo.patch("*** Add File: svc/billing/invoice_sync.py", sid=HOLDOUT), {},
                       directory=repo.state) == (None, None)


def test_claude_code_holdout_never_denies_but_keeps_its_marker(tmp_path):
    env = {"CLAUDE_CODE_ENTRYPOINT": "cli", "E3_SESH_STATE_DIR": str(tmp_path)}
    search = {"hook_event_name": "PreToolUse", "tool_name": "mcp__cosmos__sesh__search_coding_sessions",
              "session_id": HOLDOUT}
    cc.start_turn({"hook_event_name": "UserPromptSubmit", "session_id": HOLDOUT}, env, tmp_path)
    assert cc.one_per_turn_denial(search, env, tmp_path) is None
    assert cc.searched_this_turn(search, env, tmp_path)
    assert cc.one_per_turn_denial(search, env, tmp_path) is None
