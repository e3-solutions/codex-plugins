from __future__ import annotations

import importlib.util
import json
import os
import time
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ("codex-session-logging", "claude-session-logging")


def load(plugin: str):
    path = ROOT / "plugins" / plugin / "scripts" / "docs_maintenance.py"
    spec = importlib.util.spec_from_file_location(f"docs_maintenance_{plugin.replace('-', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "remote", "add", "origin", "https://github.com/e3-solutions/example.git")
    (repo / "AGENTS.md").write_text("Run `python app.py --port 8080`.\n")
    (repo / "app.py").write_text("print('hi')\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return repo


@pytest.fixture(params=PLUGINS)
def dm(request, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("E3_DOCS_MAINTENANCE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("E3_DOCS_MAINTENANCE", "1")
    return load(request.param)


def big_change(repo: Path) -> None:
    (repo / "app.py").write_text("".join(f"print({i})\n" for i in range(12)))


def test_both_plugins_ship_the_same_module():
    codex, claude = (ROOT / "plugins" / p / "scripts" / "docs_maintenance.py" for p in PLUGINS)
    assert codex.read_bytes() == claude.read_bytes()


def test_blocks_once_for_a_real_code_change(dm, repo):
    big_change(repo)
    payload = {"cwd": str(repo), "session_id": "s1"}
    output = dm.decide(payload)
    assert output["decision"] == "block"
    assert dm.MARKER in output["reason"] and "`app.py`" in output["reason"]
    assert dm.decide(payload) is None  # same change set: asked once only
    events = (dm.STATE_DIR / "events.jsonl").read_text().splitlines()
    assert json.loads(events[0])["action"] == "prompted"


def test_new_code_file_counts_even_when_small(dm, repo):
    (repo / "deploy.sh").write_text("echo hi\n")
    assert dm.decide({"cwd": str(repo)})["decision"] == "block"


@pytest.mark.parametrize("case", ["flag_off", "loop", "docs_touched", "docs_only", "trivial", "not_e3", "no_agent_docs"])
def test_lets_the_agent_stop(dm, repo, monkeypatch, case):
    big_change(repo)
    payload = {"cwd": str(repo)}
    if case == "flag_off":
        monkeypatch.setenv("E3_DOCS_MAINTENANCE", "0")
    elif case == "loop":
        payload["stop_hook_active"] = True
    elif case == "docs_touched":
        (repo / "AGENTS.md").write_text("Run `python app.py --listen-port 9090`.\n")
    elif case == "docs_only":
        git(repo, "checkout", "--", "app.py")
        (repo / "docs").mkdir()
        (repo / "docs" / "notes.md").write_text("x\n")
    elif case == "trivial":
        (repo / "app.py").write_text("print('hello')\n")
    elif case == "not_e3":
        git(repo, "remote", "set-url", "origin", "https://github.com/someone/else.git")
    elif case == "no_agent_docs":
        (repo / "AGENTS.md").unlink()
        git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "drop docs")
        big_change(repo)
    assert dm.decide(payload) is None


def test_flag_file_enables_and_env_zero_disables(tmp_path, monkeypatch):
    monkeypatch.setenv("E3_DOCS_MAINTENANCE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("E3_DOCS_MAINTENANCE", raising=False)
    module = load("codex-session-logging")
    assert module.enabled() is False
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "enabled").write_text("")
    assert module.enabled() is True
    monkeypatch.setenv("E3_DOCS_MAINTENANCE", "0")
    assert module.enabled() is False


def test_run_never_raises_and_prints_nothing_when_off(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("E3_DOCS_MAINTENANCE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("E3_DOCS_MAINTENANCE", raising=False)
    module = load("claude-session-logging")
    module.run({"cwd": str(tmp_path / "missing")})
    assert capsys.readouterr().out == ""


def test_more_edits_to_the_same_files_do_not_ask_again(dm, repo):
    big_change(repo)
    assert dm.decide({"cwd": str(repo), "session_id": "s"})["decision"] == "block"
    (repo / "app.py").write_text("".join(f"print({i})\n" for i in range(40)))
    assert dm.decide({"cwd": str(repo), "session_id": "s"}) is None


def test_a_new_code_file_asks_again_up_to_the_session_cap(dm, repo):
    big_change(repo)
    assert dm.decide({"cwd": str(repo), "session_id": "s"})["decision"] == "block"
    (repo / "server.py").write_text("x = 1\n")
    assert dm.decide({"cwd": str(repo), "session_id": "s"})["decision"] == "block"
    (repo / "worker.py").write_text("y = 1\n")
    assert dm.decide({"cwd": str(repo), "session_id": "s"}) is None  # MAX_PROMPTS_PER_SESSION


def test_untracked_non_code_files_are_ignored(dm, repo):
    (repo / "notes.txt").write_text("scratch\n")
    (repo / "run.log").write_text("log\n")
    assert dm.decide({"cwd": str(repo)}) is None


def test_changes_older_than_the_session_are_ignored(dm, repo, tmp_path):
    big_change(repo)
    old = time.time() - 3600
    os.utime(repo / "app.py", (old, old))
    transcript = tmp_path / "t.jsonl"
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    transcript.write_text(json.dumps({"timestamp": f"{stamp}.000Z", "type": "user"}) + "\n")
    assert dm.decide({"cwd": str(repo), "transcript_path": str(transcript)}) is None
    (repo / "app.py").write_text("".join(f"print({i})\n" for i in range(15)))
    assert dm.decide({"cwd": str(repo), "transcript_path": str(transcript)})["decision"] == "block"


def test_prompt_keeps_the_original_answer(dm, repo):
    big_change(repo)
    reason = dm.decide({"cwd": str(repo)})["reason"]
    assert "repeat your previous final answer" in reason
    assert "do not commit or push only to add the trailer" in reason
