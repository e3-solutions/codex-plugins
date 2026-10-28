"""COR-4900: opt-in allow-listed directories for E3 work outside any git repo.

Off by default. When a user lists a directory, sessions whose cwd is under it and
has no git origin are captured, get SessionStart guidance and the Forum cue, and
upload a fixed in-org marker as repo_remote instead of any path.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CLAUDE = ROOT / "plugins" / "claude-session-logging" / "scripts"
CODEX = ROOT / "plugins" / "codex-session-logging" / "scripts"
MARKER = "https://github.com/e3-solutions/allowlisted-dir.git"
SHARED = ("session_logging", "publish_presence")


def load(directory: Path, name: str, alias: str | None = None):
    spec = importlib.util.spec_from_file_location(alias or name, directory / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[alias or name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    saved = {name: sys.modules.get(name) for name in SHARED}
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("CLAUDE_SESSION_LOG_STATE_DIR", str(tmp_path / "claude-state"))
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "codex-state"))
    monkeypatch.setenv("CLAUDE_SESSION_LOG_AUTO_UPLOAD", "0")
    monkeypatch.setenv("CODEX_SESSION_LOG_AUTO_UPLOAD", "0")
    for env in ("CLAUDE_SESSION_LOG_EXTRA_DIRS", "CODEX_SESSION_LOG_EXTRA_DIRS",
                "CLAUDE_SESSION_LOG_ALLOWED_GITHUB_ORG", "FORUM_CUE_ENABLED", "FORUM_CUE_AB"):
        monkeypatch.delenv(env, raising=False)
    yield
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


@pytest.fixture
def scratch(tmp_path):
    """A non-git working directory like ~/code/xloop, plus a nested subdir."""
    root = tmp_path / "xloop"
    (root / "sub").mkdir(parents=True)
    return root


def git_repo(path: Path, origin: str | None) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if origin:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    return path


@pytest.fixture(params=["claude", "codex"])
def plugin(request):
    if request.param == "claude":
        module = load(CLAUDE, "session_logging")
        return types_ns(module, "CLAUDE")
    module = load(CODEX, "session_logging")
    return types_ns(module, "CODEX")


def types_ns(module, prefix):
    module.TEST_ENV = f"{prefix}_SESSION_LOG_EXTRA_DIRS"
    module.TEST_STATE_ENV = f"{prefix}_SESSION_LOG_STATE_DIR"
    return module


def capture_payload(cwd: Path, **extra) -> dict:
    return {"session_id": "11111111-2222-4333-8444-555555555555", "cwd": str(cwd), **extra}


def test_off_by_default_non_repo_dir_is_not_captured(plugin, scratch):
    assert plugin.allowlisted_dirs() == ()
    assert plugin.session_repo_remote(str(scratch)) is None
    assert not plugin.should_capture_payload(capture_payload(scratch))
    assert not plugin.should_prompt_collective(capture_payload(scratch, source="startup"))


def test_env_allow_list_captures_with_fixed_marker_not_path(plugin, scratch, monkeypatch):
    monkeypatch.setenv(plugin.TEST_ENV, f"/does/not/matter:{scratch}")
    for cwd in (scratch, scratch / "sub"):
        assert plugin.session_repo_remote(str(cwd)) == MARKER
        assert plugin.should_capture_payload(capture_payload(cwd))
        assert plugin.should_prompt_collective(capture_payload(cwd, source="startup"))
    assert not plugin.should_prompt_collective(capture_payload(scratch, source="clear"))


def test_state_file_allow_list(plugin, scratch, monkeypatch, tmp_path):
    state = Path(plugin.state_dir())
    state.mkdir(parents=True, exist_ok=True)
    (state / "extra-dirs").write_text(f"# E3 scratch\n\n{scratch}\n", encoding="utf-8")
    assert plugin.should_capture_payload(capture_payload(scratch / "sub"))


def test_prefix_sibling_and_unsafe_entries_do_not_match(plugin, scratch, monkeypatch, tmp_path):
    sibling = tmp_path / "xloop-other"
    sibling.mkdir()
    monkeypatch.setenv(plugin.TEST_ENV, f"relative/xloop:/:{Path.home()}:~")
    assert plugin.allowlisted_dirs() == ()
    assert not plugin.should_capture_payload(capture_payload(scratch))
    monkeypatch.setenv(plugin.TEST_ENV, str(scratch))
    assert not plugin.should_capture_payload(capture_payload(sibling))
    assert not plugin.should_capture_payload(capture_payload(tmp_path))


def test_real_origin_wins_inside_allow_listed_dir(plugin, scratch, monkeypatch):
    monkeypatch.setenv(plugin.TEST_ENV, str(scratch))
    personal = git_repo(scratch / "personal", "https://github.com/someone-else/side-project.git")
    assert plugin.session_repo_remote(str(personal)) == "https://github.com/someone-else/side-project.git"
    assert not plugin.should_capture_payload(capture_payload(personal))
    e3 = git_repo(scratch / "e3", "https://github.com/e3-solutions/sesh.git")
    assert plugin.session_repo_remote(str(e3)) == "https://github.com/e3-solutions/sesh.git"
    no_origin = git_repo(scratch / "local-only", None)
    assert plugin.session_repo_remote(str(no_origin)) == MARKER


def test_client_context_uploads_marker_and_no_extra_path(plugin, scratch, monkeypatch, tmp_path):
    monkeypatch.setenv(plugin.TEST_ENV, str(scratch))
    record = {"metadata": {"cwd": str(scratch)}}
    context = plugin.client_context(record, base=tmp_path / "base")
    assert context["repo_remote"] == MARKER
    assert str(scratch) not in context["repo_remote"]
    assert "git_branch" not in context


def test_ingest_validation_accepts_marker_only_while_opted_in(plugin, scratch, monkeypatch):
    payload = {"client": {"repo_remote": MARKER}, "record": {"type": "event"}}
    with pytest.raises(plugin.PermanentUploadError):
        plugin.validate_ingest_payload(payload)
    monkeypatch.setenv(plugin.TEST_ENV, str(scratch))
    plugin.validate_ingest_payload(payload)
    # Ordinary E3 remotes are unaffected either way.
    monkeypatch.delenv(plugin.TEST_ENV)
    plugin.validate_ingest_payload({"client": {"repo_remote": "https://github.com/e3-solutions/sesh.git"},
                                    "record": {"type": "event"}})


def test_claude_capture_writes_marker_end_to_end(scratch, monkeypatch, tmp_path):
    module = load(CLAUDE, "session_logging")
    payload = capture_payload(scratch, hook_event_name="SessionStart", source="startup")
    assert module.capture_hook_event(dict(payload), event_name="SessionStart") is None
    monkeypatch.setenv("CLAUDE_SESSION_LOG_EXTRA_DIRS", str(scratch))
    captured = module.capture_hook_event(dict(payload), event_name="SessionStart")
    assert captured and captured["event_type"] == "thread_started"
    base = tmp_path / "claude-state"
    upload = module.build_ingest_payload(captured, base=base)
    module.validate_ingest_payload(upload)
    assert upload["client"]["repo_remote"] == MARKER
    assert upload["record"]["metadata"]["cwd"] == str(scratch)  # same cwd field every session already sends


def test_claude_presence_target_uses_marker(scratch, monkeypatch, tmp_path):
    load(CLAUDE, "session_logging")
    presence = load(CLAUDE, "publish_presence")
    transcript = tmp_path / "11111111-2222-4333-8444-555555555555.jsonl"
    transcript.write_text(json.dumps({"cwd": str(scratch), "gitBranch": None}) + "\n", encoding="utf-8")
    assert presence.session_target(transcript) is None
    monkeypatch.setenv("CLAUDE_SESSION_LOG_EXTRA_DIRS", str(scratch))
    assert presence.session_target(transcript)["repo_remote"] == MARKER


@pytest.mark.parametrize("directory,env", [(CLAUDE, "CLAUDE_SESSION_LOG_EXTRA_DIRS"),
                                           (CODEX, "CODEX_SESSION_LOG_EXTRA_DIRS")])
def test_forum_cue_fires_in_allow_listed_dir_only_when_opted_in(directory, env, scratch, monkeypatch, tmp_path):
    cue = load(directory, "forum_cue", alias=f"forum_cue_extra_{directory.parent.name}")
    monkeypatch.setenv("FORUM_CUE_ENABLED", "1")  # COR-5034: the cue is off unless switched on.
    prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "s-extra", "cwd": str(scratch),
              "prompt": "fix the timeout"}
    assert cue.forum_cue(prompt, directory=tmp_path / "off") is None
    monkeypatch.setenv(env, str(scratch))
    assert cue.forum_cue(prompt, directory=tmp_path / "on")
    other = "CODEX_SESSION_LOG_EXTRA_DIRS" if env.startswith("CLAUDE") else "CLAUDE_SESSION_LOG_EXTRA_DIRS"
    monkeypatch.delenv(env)
    monkeypatch.setenv(other, str(scratch))
    assert cue.forum_cue(prompt, directory=tmp_path / "other-agent") is None


def test_forum_cue_state_file(scratch, monkeypatch, tmp_path):
    cue = load(CLAUDE, "forum_cue", alias="forum_cue_extra_file")
    monkeypatch.setenv("FORUM_CUE_ENABLED", "1")  # COR-5034: the cue is off unless switched on.
    state = tmp_path / "claude-state"
    state.mkdir()
    (state / "extra-dirs").write_text(f"{scratch}\n", encoding="utf-8")
    prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "s-file", "cwd": str(scratch / "sub"),
              "prompt": "fix the timeout"}
    assert cue.forum_cue(prompt, directory=tmp_path / "cue")


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_session_start_guidance_scope(agent, scratch, monkeypatch):
    feedback = load(CLAUDE, "collective_feedback", alias=f"collective_feedback_extra_{agent}")
    assert not feedback._e3_repository(str(scratch), agent)
    monkeypatch.setenv(f"{agent.upper()}_SESSION_LOG_EXTRA_DIRS", str(scratch))
    assert feedback._e3_repository(str(scratch / "sub"), agent)
    assert not feedback._e3_repository(str(scratch), "sherlock")
    other = types.SimpleNamespace(returncode=0, stdout="https://github.com/someone-else/side-project.git\n")
    monkeypatch.setattr(feedback.subprocess, "run", lambda *a, **k: other)
    assert not feedback._e3_repository(str(scratch / "sub"), agent)
