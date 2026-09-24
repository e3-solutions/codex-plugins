"""Local process/packaging acceptance: no credentials, network or installed profiles."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHERLOCK = Path(os.environ["SHERLOCK_FEEDBACK_TEST_ROOT"]) if os.environ.get("SHERLOCK_FEEDBACK_TEST_ROOT") else None
PROVIDERS = ["codex-session-logging", "claude-session-logging"]
if SHERLOCK:
    PROVIDERS += ["sherlock", "sherlock-claude-code"]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


def provider_root(name):
    return (SHERLOCK if name.startswith("sherlock") else ROOT) / "plugins" / name


@pytest.fixture
def local(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                    "https://github.com/e3-solutions/local-fixture.git"], check=True)
    home = tmp_path / "home"
    home.mkdir()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    (binaries / "python3").symlink_to(sys.executable)
    guard = tmp_path / "guard"
    guard.mkdir()
    # Python child processes inherit this guard. Block/record network attempts;
    # stub only optional updater/presence work, not hook dispatch or context logic.
    (guard / "sitecustomize.py").write_text(
        "import os,sys,socket,types\n"
        "from pathlib import Path\n"
        "if os.environ.get('TEST_DUP_OUTPUT')=='1': os.dup(1)\n"
        "def deny(*a,**k):\n"
        " p=Path(os.environ['TEST_NETWORK_LOG']); p.touch(); raise RuntimeError('network denied')\n"
        "socket.socket.connect=deny\nsocket.socket.connect_ex=deny\nsocket.create_connection=deny\n"
        "for name,method in [('update_plugin','maybe_spawn_auto_update'),('presence_ticker','spawn')]:\n"
        " m=types.ModuleType(name); setattr(m,method,lambda *a,**k:None); sys.modules[name]=m\n",
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime" / "sherlock_collector"
    runtime.mkdir(parents=True)
    (runtime / "__init__.py").write_text("", encoding="utf-8")
    (runtime / "cli.py").write_text(
        "import os,sys,time\nfrom pathlib import Path\n"
        "def main(args):\n"
        " time.sleep(float(os.environ.get('TEST_CAPTURE_DELAY','0')))\n"
        " data=sys.stdin.buffer.read() if hasattr(sys.stdin,'buffer') else sys.stdin.read().encode()\n"
        " Path(os.environ['TEST_CAPTURE']).write_bytes(data)\n"
        " if os.environ.get('TEST_CAPTURE_FAIL')=='1': raise RuntimeError('synthetic failure')\n"
        " print('synthetic telemetry output')\nreturn_value=0\n", encoding="utf-8",
    )
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"fixture":1}\n', encoding="utf-8")
    env = {
        "PATH": str(binaries) + os.pathsep + os.environ["PATH"], "HOME": str(home), "PYTHONPATH": str(guard),
        "CODEX_HOME": str(home / ".codex"), "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "E3_COLLECTIVE_FEEDBACK_HOOK_ENABLED": "1", "E3_COLLECTIVE_HOOK_ENABLED": "1",
        "E3_COLLECTIVE_HOOK_STATE_DIR": str(tmp_path / "context-state"),
        "CODEX_SESSION_LOG_AUTO_UPLOAD": "0", "CLAUDE_SESSION_LOG_AUTO_UPLOAD": "0",
        "CLAUDE_SESSION_LOG_AUTO_UPDATE": "0",
        "CODEX_SESSION_LOG_STATE_DIR": str(tmp_path / "codex-state"),
        "CLAUDE_SESSION_LOG_STATE_DIR": str(tmp_path / "claude-state"),
        "SHERLOCK_COLLECTOR_SOURCE": str(runtime.parent),
        "TEST_NETWORK_LOG": str(tmp_path / "network-attempt"),
    }
    payload = {"session_id": "synthetic-session", "source": "startup",
               "cwd": str(repo), "hook_event_name": "SessionStart",
               "transcript_path": str(transcript)}
    yield tmp_path, env, payload
    assert not Path(env["TEST_NETWORK_LOG"]).exists(), "local hooks attempted network access"


def package(name, local):
    tmp, env, _ = local
    source = provider_root(name)
    if name == "codex-session-logging":
        target = Path(env["CODEX_HOME"]) / "plugins/cache/test" / name / "preview"
        if target.exists():
            return target
        updater = load(ROOT / "plugins/linear-progress-sync/scripts/update_plugin.py", "fixture_updater")
        return updater.install_plugin_dir(source, cache_parent=Path(env["CODEX_HOME"]) /
                                           "plugins/cache/test" / name, version="preview")
    target = tmp / "packages" / name
    if name.startswith("sherlock"):
        staging = load(SHERLOCK / "plugins/sherlock/scripts/stage_marketplace.py", "fixture_staging")
        marketplace = tmp / "sherlock-marketplace"
        if not marketplace.exists():
            staging.copy_marketplace(SHERLOCK, marketplace)
        return marketplace / "plugins" / name
    if not target.exists():
        shutil.copytree(source, target)
    return target


def invoke(name, local, event="SessionStart", payload=None, raw=None):
    tmp, base_env, default_payload = local
    target = package(name, local)
    manifest = json.loads((target / "hooks/hooks.json").read_text())["hooks"]
    if event not in manifest:
        pytest.skip(f"{name} does not register {event}")
    handler = manifest[event][0]["hooks"][0]
    env = {**base_env, "CLAUDE_PLUGIN_ROOT": str(target),
           "TEST_CAPTURE": str(tmp / f"capture-{name}")}
    Path(env["TEST_CAPTURE"]).unlink(missing_ok=True)
    if name == "sherlock":
        cache = Path(env["CODEX_HOME"]) / "plugins/cache/test/sherlock/preview"
        if not cache.exists():
            shutil.copytree(target, cache)
    command = handler["command"]
    if handler.get("args"):
        # Codex's array-form command runner expands plugin-root placeholders.
        args = [a.replace("${CLAUDE_PLUGIN_ROOT}", str(target)) for a in handler["args"]]
        argv = [command, *args]
    else:
        argv = ["sh", "-c", command]
    data = raw if raw is not None else json.dumps(payload if payload is not None else default_payload).encode()
    result = subprocess.run(argv, input=data, capture_output=True, env=env, cwd=tmp, timeout=20)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    if name.startswith("sherlock"):
        receipt = Path(env["TEST_CAPTURE"])
        for _ in range(500):
            if receipt.exists() and receipt.read_bytes() == data:
                break
            time.sleep(0.01)
        assert receipt.read_bytes() == data, "guidance must preserve telemetry stdin exactly"
    output = result.stdout.decode()
    if name == "sherlock":
        response = json.loads(output)
        assert response["continue"] is True
        output = response.get("hookSpecificOutput", {}).get("additionalContext", "")
    return output.strip()


@pytest.mark.parametrize("name", PROVIDERS)
@pytest.mark.parametrize("source", ["startup", "resume", "clear", "compact"])
def test_packaged_context_loads(name, source, local):
    local[2]["source"] = source
    output = invoke(name, local)
    assert output.count("E3 Collective / Forum:") == 1
    assert output.endswith("do not seek broader access.")


@pytest.mark.parametrize("name", PROVIDERS)
def test_released_default_and_explicit_global_optout(name, local):
    local[1].pop("E3_COLLECTIVE_FEEDBACK_HOOK_ENABLED")
    assert "E3 Collective / Forum:" in invoke(name, local)
    local[2]["session_id"] = "optout-session"
    local[1]["E3_COLLECTIVE_HOOK_ENABLED"] = "0"
    assert "E3 Collective" not in invoke(name, local)


@pytest.mark.parametrize("name", PROVIDERS)
@pytest.mark.parametrize("flag", ["", "0", "true", "1"])
def test_explicit_flag_and_overriding_optout(name, flag, local):
    local[1]["E3_COLLECTIVE_FEEDBACK_HOOK_ENABLED"] = flag
    if flag == "1":
        local[1]["E3_COLLECTIVE_HOOK_ENABLED"] = "0"
    output = invoke(name, local)
    assert "E3 Collective / Forum:" not in output
    if not name.startswith("sherlock") and flag != "1":
        assert "E3 Collective:" in output  # Legacy unchanged.


@pytest.mark.parametrize("name", PROVIDERS)
@pytest.mark.parametrize("source", [None, "unknown", [], 3])
def test_invalid_sources_fail_soft(name, source, local):
    local[2]["source"] = source
    assert "E3 Collective" not in invoke(name, local)


@pytest.mark.parametrize("name", PROVIDERS)
def test_non_context_events_never_prompt(name, local):
    events = json.loads((provider_root(name) / "hooks/hooks.json").read_text())["hooks"]
    for event in events:
        if event == "SessionStart":
            continue
        payload = {**local[2], "hook_event_name": event, "tool_name": "Read",
                   "last_assistant_message": "Substantive reusable result with evidence"}
        output = invoke(name, local, event, payload)
        if event == "UserPromptSubmit" and name in ("codex-session-logging", "claude-session-logging"):
            # Only the decision-time Forum cue; never contribution/feedback guidance.
            assert "E3 Collective / Forum:" not in output and "E3 Collective:" not in output
            assert "share_with_collective" not in output
            continue
        assert "E3 Collective" not in output


@pytest.mark.parametrize("remote", [
    "https://github.com.evil.test/e3-solutions/repo",
    "https://github.com/other/repo", "git@evil.test:e3-solutions/repo",
    "https://github.com/e3-solutions-else/repo", "/tmp/e3-solutions/repo",
])
def test_scope_is_not_substring_or_logging_override(remote, local):
    subprocess.run(["git", "-C", local[2]["cwd"], "remote", "set-url", "origin", remote], check=True)
    local[1]["CLAUDE_SESSION_LOG_ALLOWED_GITHUB_ORG"] = "other"
    for name in PROVIDERS:
        assert "E3 Collective" not in invoke(name, local)


@pytest.mark.parametrize("remote", [
    "git@github.com:e3-solutions/repo.git", "ssh://git@github.com/e3-solutions/repo.git",
])
def test_canonical_ssh_scope(remote, local):
    subprocess.run(["git", "-C", local[2]["cwd"], "remote", "set-url", "origin", remote], check=True)
    assert "E3 Collective / Forum:" in invoke("codex-session-logging", local)


def test_guidance_semantics_and_all_shipped_copies_match():
    sources = [(provider_root(name) / "scripts/collective_feedback.py").read_bytes() for name in PROVIDERS]
    assert all(source == sources[0] for source in sources)
    module = load(provider_root(PROVIDERS[0]) / "scripts/collective_feedback.py", "context_contract")
    text = module.FEEDBACK_CONTEXT
    assert len(text) < 4000
    for phrase in [
        "tools being listed is not proof", "only when enabled and authorized",
        "Submission stays private", "candidate AND files with E3",
        "not public publication", "After fully reading each Forum post",
        "before completing the task", "kind=up", "kind=down", "kind=abstain",
        "post_id, body_sha256, explanation, and mutation_id",
        "No positive or negative sentiment is forced",
        "Concern is optional, separate, and only for a specific issue",
        "One current vote per authenticated Cosmos user",
        "USE/SKIP line or prose report is not a saved feedback event",
        "same mutation_id and identical payload", "feedback remains unsaved",
        "pending is not a quality defect", "Only a designated librarian",
        "without publishing", "finish normally",
    ]:
        assert phrase in text
    assert all(word not in sources[0].decode() for word in ["import requests", "import urllib", "urlopen("])


@pytest.mark.skipif(SHERLOCK is None, reason="set SHERLOCK_FEEDBACK_TEST_ROOT for cross-provider integration")
@pytest.mark.parametrize("agent,providers", [
    ("codex", ["codex-session-logging", "sherlock"]),
    ("claude", ["claude-session-logging", "sherlock-claude-code"]),
])
@pytest.mark.parametrize("concurrent", [False, True])
def test_coinstalled_once_and_compaction_reinjection(agent, providers, concurrent, local):
    for name in providers:
        package(name, local)
    def outputs():
        if concurrent:
            with ThreadPoolExecutor(max_workers=2) as pool:
                return list(pool.map(lambda name: invoke(name, local), providers))
        return [invoke(name, local) for name in reversed(providers)]
    assert sum("E3 Collective / Forum:" in out for out in outputs()) == 1
    local[2]["source"] = "compact"
    assert sum("E3 Collective / Forum:" in out for out in outputs()) == 1
    # Another actual transcript revision is a new load, even immediately.
    Path(local[2]["transcript_path"]).write_text('{"fixture":2,"new_context":true}\n')
    assert sum("E3 Collective / Forum:" in out for out in outputs()) == 1
    local[2]["session_id"] = "another-session"
    assert sum("E3 Collective / Forum:" in out for out in outputs()) == 1


def test_duplicate_without_transcript_has_documented_bounded_debounce(local, monkeypatch):
    module = load(provider_root(PROVIDERS[0]) / "scripts/collective_feedback.py", "dedup_test")
    monkeypatch.setenv("E3_COLLECTIVE_FEEDBACK_HOOK_ENABLED", "1")
    monkeypatch.setenv("E3_COLLECTIVE_HOOK_STATE_DIR", local[1]["E3_COLLECTIVE_HOOK_STATE_DIR"])
    local[2].pop("transcript_path")
    assert module.feedback_context(local[2], event_name="SessionStart", agent="codex")
    assert module.feedback_context(local[2], event_name="SessionStart", agent="codex") is None
    with sqlite3.connect(Path(local[1]["E3_COLLECTIVE_HOOK_STATE_DIR"]) / "context.sqlite3") as db:
        db.execute("UPDATE cues SET emitted = emitted - 31")
    assert module.feedback_context(local[2], event_name="SessionStart", agent="codex")


@pytest.mark.parametrize("name", PROVIDERS)
def test_guidance_dependency_failure_preserves_telemetry(name, local):
    target = package(name, local)
    (target / "scripts/collective_feedback.py").write_text("raise RuntimeError('synthetic module failure')\n")
    output = invoke(name, local)
    assert "E3 Collective" not in output
    if not name.startswith("sherlock"):
        state = Path(local[1]["CODEX_SESSION_LOG_STATE_DIR" if name.startswith("codex") else "CLAUDE_SESSION_LOG_STATE_DIR"])
        assert (state / "events.jsonl").exists()


@pytest.mark.parametrize("name", [n for n in PROVIDERS if n.startswith("sherlock")])
@pytest.mark.parametrize("raw", [b'not JSON \xff', b'["wrong shape"]', b'{"source":[]}'])
def test_sherlock_replays_even_malformed_bytes(name, raw, local):
    assert "E3 Collective" not in invoke(name, local, raw=raw)


@pytest.mark.parametrize("name", PROVIDERS)
def test_capture_failure_does_not_discard_context(name, local):
    if name.startswith("sherlock"):
        local[1]["TEST_CAPTURE_FAIL"] = "1"
    else:
        broken = local[0] / "broken-state"
        broken.write_text("not a directory")
        local[1]["CODEX_SESSION_LOG_STATE_DIR" if name.startswith("codex") else "CLAUDE_SESSION_LOG_STATE_DIR"] = str(broken)
    assert "E3 Collective / Forum:" in invoke(name, local)


@pytest.mark.parametrize("name", PROVIDERS)
def test_unwritable_dedup_state_preserves_normal_capture(name, local):
    broken = local[0] / "not-a-state-directory"
    broken.write_text("synthetic state failure")
    local[1]["E3_COLLECTIVE_HOOK_STATE_DIR"] = str(broken)
    assert "E3 Collective" not in invoke(name, local)
    if not name.startswith("sherlock"):
        state = Path(local[1]["CODEX_SESSION_LOG_STATE_DIR" if name.startswith("codex") else "CLAUDE_SESSION_LOG_STATE_DIR"])
        assert (state / "events.jsonl").exists()


@pytest.mark.parametrize("name", PROVIDERS)
def test_missing_cwd_does_not_inherit_e3_scope(name, local):
    local[2].pop("cwd")
    assert "E3 Collective" not in invoke(name, local)


@pytest.mark.parametrize("name", PROVIDERS)
def test_explicit_rollback_preserves_baseline_and_no_preview_state(name, local):
    local[1]["E3_COLLECTIVE_FEEDBACK_HOOK_ENABLED"] = "0"
    output = invoke(name, local)
    assert "E3 Collective / Forum:" not in output
    assert not Path(local[1]["E3_COLLECTIVE_HOOK_STATE_DIR"]).exists()


@pytest.mark.skipif(SHERLOCK is None, reason="requires companion Sherlock worktree")
def test_claude_replay_larger_than_pipe_capacity(local):
    payload = {**local[2], "synthetic_evidence": "large Unicode fixture Ω " * 20000}
    assert "E3 Collective / Forum:" in invoke("sherlock-claude-code", local, payload=payload)


@pytest.mark.skipif(SHERLOCK is None, reason="requires companion Sherlock worktree")
def test_claude_detached_replay_survives_parent_exit_with_slow_reader(local):
    local[1]["TEST_CAPTURE_DELAY"] = "3"
    local[1]["TEST_DUP_OUTPUT"] = "1"
    target = package("sherlock-claude-code", local)
    payload = json.dumps({**local[2], "fixture": "Ω" * 200000}).encode()
    receipt = local[0] / "delayed-receipt"
    env = {**local[1], "TEST_CAPTURE": str(receipt)}
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(target / "scripts/run_hook.py"), "SessionStart"],
        input=payload, capture_output=True, env=env, timeout=2,
    )
    assert result.returncode == 0
    assert time.monotonic() - started < 2
    for _ in range(500):
        if receipt.exists() and receipt.read_bytes() == payload:
            break
        time.sleep(0.01)
    assert receipt.read_bytes() == payload
