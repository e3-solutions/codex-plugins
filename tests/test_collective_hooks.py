from __future__ import annotations

import ast
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CODEX_SCRIPTS = ROOT / "plugins" / "codex-session-logging" / "scripts"
CLAUDE_SCRIPTS = ROOT / "plugins" / "claude-session-logging" / "scripts"


def load_collective(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path / "collective.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=[CODEX_SCRIPTS, CLAUDE_SCRIPTS], ids=["codex", "claude"])
def collective(request):
    return load_collective(request.param, f"collective_{request.param.parent.name}")


def init_git_repo(path: Path, remote: str) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", remote], cwd=path, check=True)
    return path


def base_env(tmp_path: Path, *, agent: str) -> dict[str, str]:
    env = dict(os.environ)
    env["E3_COLLECTIVE_HOOK_ENABLED"] = "1"
    if agent == "codex":
        env.update(
            {
                "CODEX_HOME": str(tmp_path / "codex-home"),
                "CODEX_SESSION_LOG_AUTO_UPLOAD": "0",
                "CODEX_SESSION_LOG_STATE_DIR": str(tmp_path / "codex-state"),
            }
        )
    else:
        env.update(
            {
                "CLAUDE_SESSION_LOG_AUTO_UPDATE": "0",
                "CLAUDE_SESSION_LOG_AUTO_UPLOAD": "0",
                "CLAUDE_SESSION_LOG_STATE_DIR": str(tmp_path / "claude-state"),
            }
        )
    return env


def run_hook(
    scripts: Path,
    hook: str,
    payload: dict,
    *,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(scripts / f"{hook}.py")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env=env,
        timeout=20,
    )


def substantive_message() -> str:
    return (
        "Implemented the bounded Collective hook and verified its loop guard with focused tests. "
        "The source is e3-solutions/codex-plugins PR #62, and the main reusable result is that the "
        "existing resident updater can distribute session context without a per-repository edit."
    )


def test_agent_collective_modules_are_identical_and_have_no_network_imports():
    codex_source = (CODEX_SCRIPTS / "collective.py").read_text(encoding="utf-8")
    claude_source = (CLAUDE_SCRIPTS / "collective.py").read_text(encoding="utf-8")
    assert codex_source == claude_source

    tree = ast.parse(codex_source)
    imported = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert imported <= {"__future__", "os", "re", "typing"}
    assert "submit" not in {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


def test_session_context_is_eligible_and_opt_out_bounded(collective, monkeypatch):
    monkeypatch.delenv("E3_COLLECTIVE_HOOK_ENABLED", raising=False)
    context = collective.session_context(eligible=True)
    assert "private review queue" in context
    assert "If Forum is unavailable" in context
    assert collective.session_context(eligible=False) is None

    monkeypatch.setenv("E3_COLLECTIVE_HOOK_ENABLED", "0")
    assert collective.session_context(eligible=True) is None


@pytest.mark.parametrize(
    "agent,scripts",
    [("codex", CODEX_SCRIPTS), ("claude", CLAUDE_SCRIPTS)],
)
@pytest.mark.parametrize("source", ["startup", "resume", "compact"])
def test_session_start_process_injects_context_for_e3_even_if_logging_fails(
    tmp_path,
    agent,
    scripts,
    source,
):
    repo = init_git_repo(tmp_path / f"{agent}-repo", "https://github.com/e3-solutions/example.git")
    env = base_env(tmp_path, agent=agent)
    state_key = "CODEX_SESSION_LOG_STATE_DIR" if agent == "codex" else "CLAUDE_SESSION_LOG_STATE_DIR"
    broken_state = tmp_path / f"{agent}-state-file"
    broken_state.write_text("not a directory", encoding="utf-8")
    env[state_key] = str(broken_state)

    result = run_hook(
        scripts,
        "session_start",
        {
            "hook_event_name": "SessionStart",
            "session_id": "test-session",
            "cwd": str(repo),
            "source": source,
        },
        env=env,
    )

    assert result.returncode == 0
    assert "E3 Collective:" in result.stdout
    assert "capture failed" in result.stderr


@pytest.mark.parametrize(
    "agent,scripts",
    [("codex", CODEX_SCRIPTS), ("claude", CLAUDE_SCRIPTS)],
)
def test_stop_process_is_silent_for_substantive_completion(
    tmp_path,
    agent,
    scripts,
):
    repo = init_git_repo(tmp_path / f"{agent}-repo", "git@github.com:e3-solutions/example.git")
    env = base_env(tmp_path, agent=agent)
    payload = {
        "hook_event_name": "Stop",
        "session_id": "test-session",
        "cwd": str(repo),
        "last_assistant_message": substantive_message(),
    }

    result = run_hook(scripts, "stop", payload, env=env)
    assert result.returncode == 0
    assert result.stdout == ""
    state_dir = tmp_path / ("codex-state" if agent == "codex" else "claude-state")
    events = [
        json.loads(line)
        for line in (state_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(event["hook_event_name"] == "Stop" for event in events)


@pytest.mark.parametrize(
    "agent,scripts",
    [("codex", CODEX_SCRIPTS), ("claude", CLAUDE_SCRIPTS)],
)
def test_hook_processes_are_silent_outside_e3(tmp_path, agent, scripts):
    repo = init_git_repo(tmp_path / f"{agent}-repo", "https://github.com/example/project.git")
    env = base_env(tmp_path, agent=agent)
    start = run_hook(
        scripts,
        "session_start",
        {"hook_event_name": "SessionStart", "session_id": "test-session", "cwd": str(repo)},
        env=env,
    )
    stop = run_hook(
        scripts,
        "stop",
        {
            "hook_event_name": "Stop",
            "session_id": "test-session",
            "cwd": str(repo),
            "last_assistant_message": substantive_message(),
        },
        env=env,
    )
    assert start.returncode == stop.returncode == 0
    assert start.stdout == stop.stdout == ""


def test_claude_logging_org_override_cannot_expand_collective_scope(tmp_path):
    repo = init_git_repo(tmp_path / "claude-repo", "https://github.com/example/project.git")
    env = base_env(tmp_path, agent="claude")
    env["CLAUDE_SESSION_LOG_ALLOWED_GITHUB_ORG"] = "example"
    payload = {
        "session_id": "test-session",
        "cwd": str(repo),
        "last_assistant_message": substantive_message(),
    }

    start = run_hook(
        CLAUDE_SCRIPTS,
        "session_start",
        {**payload, "hook_event_name": "SessionStart"},
        env=env,
    )
    stop = run_hook(
        CLAUDE_SCRIPTS,
        "stop",
        {**payload, "hook_event_name": "Stop"},
        env=env,
    )

    assert start.returncode == stop.returncode == 0
    assert start.stdout == stop.stdout == ""
    assert (tmp_path / "claude-state" / "events.jsonl").exists()
