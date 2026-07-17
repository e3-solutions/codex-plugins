from __future__ import annotations

import importlib.util
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "plugins" / "linear-progress-sync" / "scripts" / "linear_sync.py"
spec = importlib.util.spec_from_file_location("linear_sync", MODULE_PATH)
linear_sync = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = linear_sync
spec.loader.exec_module(linear_sync)

SETUP_PATH = ROOT / "plugins" / "linear-progress-sync" / "scripts" / "setup.py"
setup_spec = importlib.util.spec_from_file_location("linear_setup", SETUP_PATH)
linear_setup = importlib.util.module_from_spec(setup_spec)
assert setup_spec.loader is not None
setup_spec.loader.exec_module(linear_setup)

UPDATE_PATH = ROOT / "plugins" / "linear-progress-sync" / "scripts" / "update_plugin.py"
RESIDENT_UPDATE_PATH = ROOT / "plugins" / "linear-progress-sync" / "scripts" / "resident_updater.py"


def load_update_plugin():
    resident_spec = importlib.util.spec_from_file_location("resident_updater", RESIDENT_UPDATE_PATH)
    resident_updater = importlib.util.module_from_spec(resident_spec)
    assert resident_spec.loader is not None
    sys.modules[resident_spec.name] = resident_updater
    resident_spec.loader.exec_module(resident_updater)
    update_spec = importlib.util.spec_from_file_location("linear_update_plugin", UPDATE_PATH)
    update_plugin = importlib.util.module_from_spec(update_spec)
    assert update_spec.loader is not None
    update_spec.loader.exec_module(update_plugin)
    return update_plugin


def load_resident_updater():
    resident_spec = importlib.util.spec_from_file_location("resident_updater", RESIDENT_UPDATE_PATH)
    resident_updater = importlib.util.module_from_spec(resident_spec)
    assert resident_spec.loader is not None
    sys.modules[resident_spec.name] = resident_updater
    resident_spec.loader.exec_module(resident_updater)
    return resident_updater


def write_minimal_plugin(
    path: Path,
    *,
    version: str,
    name: str = "linear-progress-sync",
    directory_name: str | None = None,
    hook_events: tuple[str, ...] | None = None,
) -> Path:
    plugin = path / (directory_name or name)
    (plugin / ".codex-plugin").mkdir(parents=True)
    (plugin / "scripts").mkdir()
    (plugin / "hooks").mkdir()
    (plugin / ".codex-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": version,
                "hooks": "./hooks/hooks.json",
                "skills": "./skills/",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    script_name = "linear_sync.py" if name == "linear-progress-sync" else "session_start.py"
    (plugin / "scripts" / script_name).write_text(f"VERSION = {version!r}\n", encoding="utf-8")
    if name == "linear-progress-sync":
        for required_script in ("update_plugin.py", "resident_updater.py"):
            (plugin / "scripts" / required_script).write_text(
                f"VERSION = {version!r}\n",
                encoding="utf-8",
            )
    hooks = {
        event: [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": f"python3 ~/.codex/plugins/cache/coreedge-local/{name}/{version}/scripts/{script_name}",
                    }
                ]
            }
        ]
        for event in (hook_events or ())
    }
    (plugin / "hooks" / "hooks.json").write_text(json.dumps({"hooks": hooks}, indent=2) + "\n", encoding="utf-8")
    return plugin


def make_plugin_archive(tmp_path: Path, *, version: str) -> tuple[Path, str]:
    payload_root = tmp_path / "payload" / "codex-plugins-main" / "plugins"
    plugin = write_minimal_plugin(payload_root, version=version)
    (plugin / "update-manifest.json").write_text(
        json.dumps(
            {
                "version": version,
                "archive_url": "",
                "plugin_subdir": "plugins/linear-progress-sync",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    archive = tmp_path / f"linear-progress-sync-{version}.zip"
    with zipfile.ZipFile(archive, "w") as zip_file:
        for path in plugin.rglob("*"):
            zip_file.write(path, path.relative_to(tmp_path / "payload"))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    return archive, digest


def make_marketplace_archive(tmp_path: Path, *, bootstrap_version: str) -> tuple[Path, str]:
    repo_root = tmp_path / "payload" / "codex-plugins-main"
    plugins_root = repo_root / "plugins"
    write_minimal_plugin(
        plugins_root,
        name="linear-progress-sync",
        version=bootstrap_version,
        hook_events=("SessionStart", "PreToolUse"),
    )
    write_minimal_plugin(
        plugins_root,
        name="codex-session-logging",
        version="0.1.0",
        hook_events=("SessionStart", "UserPromptSubmit"),
    )
    write_minimal_plugin(
        plugins_root,
        name="internal-experiment",
        version="0.1.0",
        hook_events=("SessionStart",),
    )
    marketplace = {
        "name": "coreedge-local",
        "plugins": [
            {
                "name": "linear-progress-sync",
                "source": {"source": "local", "path": "./plugins/linear-progress-sync"},
                "policy": {"installation": "INSTALLED_BY_DEFAULT"},
            },
            {
                "name": "codex-session-logging",
                "source": {"source": "local", "path": "./plugins/codex-session-logging"},
                "policy": {"installation": "INSTALLED_BY_DEFAULT"},
            },
            {
                "name": "internal-experiment",
                "source": {"source": "local", "path": "./plugins/internal-experiment"},
                "policy": {"installation": "AVAILABLE"},
            },
        ],
    }
    (repo_root / ".agents" / "plugins").mkdir(parents=True)
    (repo_root / ".agents" / "plugins" / "marketplace.json").write_text(
        json.dumps(marketplace, indent=2) + "\n",
        encoding="utf-8",
    )
    archive = tmp_path / f"coreedge-local-{bootstrap_version}.zip"
    with zipfile.ZipFile(archive, "w") as zip_file:
        for path in repo_root.rglob("*"):
            zip_file.write(path, path.relative_to(tmp_path / "payload"))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    return archive, digest


def init_git_repo(
    path: Path,
    branch: str = "arya/cor-1-work",
    origin_url: str | None = "git@github.com:e3-solutions/codex-plugins.git",
) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "codex@example.test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Codex Test"], cwd=path, check=True)
    if origin_url:
        subprocess.run(["git", "remote", "add", "origin", origin_url], cwd=path, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-m", "init"], cwd=path, check=True)
    subprocess.run(["git", "switch", "-q", "-c", branch], cwd=path, check=True)
    return path


def add_origin(repo: Path, url: str) -> None:
    subprocess.run(["git", "remote", "add", "origin", url], cwd=repo, check=True)


def active_payload(
    repo: Path,
    *,
    issue_key: str = "COR-33",
    issue_title: str = "Active issue",
    branch: str | None = None,
    pr_number: int = 33,
) -> dict:
    return {
        "issue_key": issue_key,
        "issue_url": f"https://linear.app/coreedge/issue/{issue_key}/test",
        "issue_title": issue_title,
        "branch": branch or linear_sync.current_branch(repo),
        "repo": str(repo),
        "pr_url": f"https://github.com/e3-solutions/codex-plugins/pull/{pr_number}",
        "pr_number": pr_number,
        "linear_linked_at": "2026-07-01T00:00:00+00:00",
    }


def save_linear_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str = "Codex Test User") -> dict:
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    return linear_sync.save_linear_user_profile(linear_name=name)


def bind_linear_repo(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    linear_sync.save_linear_user_profile(linear_name="Codex Test User")
    return linear_sync.save_repo_linear_binding(
        team="Engineering",
        project="Codex Plugins",
        root=root,
    )


def test_global_linear_user_profile_round_trips(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))

    missing = linear_sync.linear_user_profile_status()
    saved = linear_sync.save_linear_user_profile(linear_name=" Arya G ")
    loaded = linear_sync.linear_user_profile_status()

    assert missing["configured"] is False
    assert saved["profile"]["linear_name"] == "Arya G"
    assert loaded["configured"] is True
    assert loaded["profile"]["linear_name"] == "Arya G"
    assert loaded["config_path"].endswith("user.json")


def test_global_linear_user_profile_rejects_blank_name(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))

    with pytest.raises(ValueError, match="Linear user profile requires linear_name"):
        linear_sync.save_linear_user_profile(linear_name=" ")


def test_terminal_statuses_are_never_changed(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
    state = linear_sync.default_state()
    state["stale_issue_cache"]["issues"] = [
        {"identifier": "COR-1", "title": "Terminal issue", "status": "Done"}
    ]
    linear_sync.save_state(state, tmp_path)
    linear_sync.enqueue_event(
        "post_commit",
        {
            "id": "evt-1",
            "branch": "nitish/cor-1-work",
            "commit_sha": "abc123",
            "commit_subject": "COR-1 useful work",
        },
        root=tmp_path,
    )
    calls = []

    def executor(prompt, event, inference):
        calls.append((prompt, event, inference))
        return linear_sync.WorkerResult(True, "should not run")

    result = linear_sync.drain_once(root=tmp_path, executor=executor)
    assert result["skipped"] == 1
    assert calls == []


def test_done_is_never_a_safe_target():
    assert linear_sync.is_safe_status_target("Done") is False
    assert linear_sync.is_safe_status_target("Completed") is False
    assert linear_sync.is_safe_status_target("In Progress") is True


def test_duplicate_commit_does_not_duplicate_comment(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
    state = linear_sync.default_state()
    linear_sync.mark_commit_synced(state, "abc123", "COR-2")
    linear_sync.save_state(state, tmp_path)
    linear_sync.enqueue_event(
        "post_commit",
        {
            "id": "evt-dup",
            "branch": "nitish/cor-2-work",
            "commit_sha": "abc123",
            "commit_subject": "COR-2 useful work",
        },
        root=tmp_path,
    )
    calls = []
    result = linear_sync.drain_once(
        root=tmp_path,
        executor=lambda prompt, event, inference: calls.append(event) or linear_sync.WorkerResult(True, "ok"),
    )
    assert result["skipped"] == 1
    assert calls == []


def test_low_confidence_issue_inference_does_not_update_linear(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
    linear_sync.enqueue_event(
        "post_commit",
        {
            "id": "evt-low",
            "branch": "feature/no-ticket",
            "commit_sha": "def456",
            "commit_subject": "Refactor helper",
        },
        root=tmp_path,
    )
    calls = []
    result = linear_sync.drain_once(
        root=tmp_path,
        executor=lambda prompt, event, inference: calls.append(event) or linear_sync.WorkerResult(True, "ok"),
    )
    assert result["skipped"] == 1
    assert calls == []


def test_branch_issue_key_wins_over_fuzzy_inference(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
    state = linear_sync.default_state()
    state["stale_issue_cache"]["issues"] = [
        {"identifier": "COR-999", "title": "Refactor helper module", "status": "In Progress"}
    ]
    event = {
        "branch": "nitish/cor-123-real-branch",
        "commit_subject": "Refactor helper module",
        "changed_files": ["training/helper.py"],
    }
    inference = linear_sync.infer_issue(event, state, root=tmp_path)
    assert inference.issue_key == "COR-123"
    assert inference.confidence >= 0.8


def test_active_linear_issue_wins_over_branch_and_commit_inference(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-33-active-issue")
    linear_sync.write_active_issue(active_payload(repo), root=repo)
    event = {
        "branch": "arya/cor-999-wrong-branch",
        "commit_subject": "COR-888 wrong commit",
    }

    inference = linear_sync.infer_issue(event, root=repo)

    assert inference.issue_key == "COR-33"
    assert inference.confidence == 1.0
    assert inference.reason == "active Linear issue state"


def test_active_state_wins_over_explicit_event_issue_key(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-33-active-issue")
    linear_sync.write_active_issue(active_payload(repo), root=repo)

    inference = linear_sync.infer_issue(
        {
            "issue_key": "COR-99",
            "branch": "arya/cor-33-active-issue",
            "commit_subject": "COR-33 active issue",
        },
        root=repo,
    )

    assert inference.issue_key == "COR-33"
    assert inference.confidence == 1.0
    assert inference.reason == "active Linear issue state"


def test_active_state_branch_mismatch_blocks_writes_and_does_not_infer_active(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    save_linear_user(tmp_path, monkeypatch)
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-99-new-work")
    linear_sync.write_active_issue(
        active_payload(repo, issue_key="COR-33", issue_title="Old work", branch="arya/cor-33-old-work"),
        root=repo,
    )

    decision = linear_sync.pre_tool_guard_decision({"tool_name": "apply_patch"}, root=repo)
    inference = linear_sync.infer_issue(
        {
            "branch": "arya/cor-99-new-work",
            "commit_subject": "Continue new work",
        },
        root=repo,
    )

    assert decision.blocked is True
    assert "does not match current branch" in decision.message
    assert inference.issue_key is None
    assert "does not match current branch" in inference.reason


def test_pre_tool_use_script_exits_2_when_blocking_write(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path / "state"))
    repo = init_git_repo(tmp_path / "repo")
    payload = {
        "cwd": str(repo),
        "hook_event_name": "PreToolUse",
        "tool_name": "apply_patch",
        "tool_input": {"command": "*** Begin Patch\n*** Update File: demo.txt\n@@\n-old\n+new\n*** End Patch\n"},
    }

    result = subprocess.run(
        [sys.executable, str(ROOT / "plugins" / "linear-progress-sync" / "scripts" / "pre_tool_use.py")],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        cwd=repo,
        check=False,
    )

    assert result.returncode == 2
    assert "LINEAR USER REQUIRED" in result.stderr
    assert "configure-user" in result.stderr


def test_active_linear_issue_state_round_trips_and_malformed_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    linear_sync.save_linear_user_profile(linear_name="Arya G")
    payload = {
        "issue_key": "COR-34",
        "issue_url": "https://linear.app/coreedge/issue/COR-34/test",
        "issue_title": "Round trip",
        "branch": "arya/cor-34-round-trip",
        "pr_url": "https://github.com/e3-solutions/codex-plugins/pull/34",
        "pr_number": 34,
        "linear_linked_at": "2026-07-01T00:00:00+00:00",
    }

    linear_sync.write_active_issue(payload, root=tmp_path)

    assert linear_sync.read_active_issue(root=tmp_path)["issue_key"] == "COR-34"
    (tmp_path / "active.json").write_text("{not json", encoding="utf-8")
    assert linear_sync.read_active_issue(root=tmp_path) is None


def test_legacy_active_state_without_link_timestamp_still_reads_when_pr_evidence_exists(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-52-legacy")
    legacy = active_payload(repo, issue_key="COR-52", issue_title="Legacy active state", pr_number=52)
    legacy.pop("linear_linked_at")
    linear_sync.write_json_atomic(linear_sync.active_issue_path(root=repo), legacy)

    active = linear_sync.read_current_active_issue(root=repo)
    missing_user = linear_sync.pre_tool_guard_decision({"tool_name": "apply_patch"}, root=repo)
    linear_sync.save_linear_user_profile(linear_name="Arya G")
    decision = linear_sync.pre_tool_guard_decision({"tool_name": "apply_patch"}, root=repo)

    assert active["issue_key"] == "COR-52"
    assert missing_user.blocked is True
    assert "LINEAR USER REQUIRED" in missing_user.message
    assert decision.blocked is False


def test_malformed_active_state_prevents_drain_linear_write(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-77-work")
    linear_sync.active_issue_path(root=repo).write_text("{not json", encoding="utf-8")
    linear_sync.enqueue_event(
        "post_commit",
        {
            "id": "evt-malformed-active",
            "commit_sha": "bad123",
            "commit_subject": "COR-77 should not fall back",
        },
        root=repo,
    )
    calls = []

    result = linear_sync.drain_once(
        root=repo,
        executor=lambda prompt, event, inference: calls.append(event) or linear_sync.WorkerResult(True, "ok"),
    )

    assert result["failed"] == 1
    assert "active_state_error" in result
    assert calls == []
    assert list((state_dir / "events").glob("*.json"))


def test_malformed_active_state_holds_foreground_sync(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-78-work")
    linear_sync.active_issue_path(root=repo).write_text("{not json", encoding="utf-8")
    linear_sync.enqueue_event(
        "post_commit",
        {"id": "evt-fg-malformed", "commit_sha": "fgbad", "commit_subject": "COR-78 hold"},
        root=repo,
    )

    plan = linear_sync.foreground_sync_plan(root=repo)

    assert plan["eligible"] == []
    assert plan["held"][0]["event_id"] == "evt-fg-malformed"
    assert "malformed" in plan["held"][0]["reason"]


def test_linear_branch_name_wins_over_fallback():
    issue = {
        "identifier": "COR-35",
        "title": "Use Linear branch naming",
        "branchName": "arya/cor-35-linear-owned-name",
    }

    assert linear_sync.linear_issue_branch_name(issue) == "arya/cor-35-linear-owned-name"
    assert linear_sync.select_branch_name(issue) == "arya/cor-35-linear-owned-name"


def test_fallback_branch_name_uses_issue_key_and_title_slug():
    assert (
        linear_sync.fallback_branch_name("COR-36", "Make Linear setup easier!")
        == "arya/COR-36-make-linear-setup-easier"
    )


def test_pr_title_and_body_link_linear_without_closing():
    title = linear_sync.pr_title_for_issue("COR-37", "Create kickoff flow")
    body = linear_sync.pr_body_for_issue(
        "COR-37",
        "Create kickoff flow",
        "https://linear.app/coreedge/issue/COR-37/create-kickoff-flow",
    )

    assert title == "COR-37: Create kickoff flow"
    assert "Refs COR-37" in body
    assert "Fixes" not in body
    assert "https://linear.app/coreedge/issue/COR-37/create-kickoff-flow" in body


def test_pre_tool_guard_blocks_writes_and_branch_creation_without_active_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    repo = init_git_repo(tmp_path / "repo")
    bind_linear_repo(repo, tmp_path, monkeypatch)
    linear_sync.save_linear_user_profile(linear_name="Arya G")

    write_decision = linear_sync.pre_tool_guard_decision({"tool_name": "apply_patch"}, root=repo)
    branch_decision = linear_sync.pre_tool_guard_decision(
        {"tool_name": "Bash", "command": "git switch -c arya/new-work"},
        root=repo,
    )

    assert write_decision.blocked is True
    assert branch_decision.blocked is True
    assert "Linear kickoff" in write_decision.message
    assert "Do not ask the user for a Linear issue key" in write_decision.message
    assert "Create a new Linear issue from the user's implementation request" in write_decision.message
    assert "Do not test write access" in write_decision.message
    assert "If a Linear issue was already created in this turn, reuse" in write_decision.message
    assert "linear_start.py kickoff" in write_decision.message
    assert "activation_command" in write_decision.message


def test_pre_tool_guard_blocks_until_global_linear_user_profile_is_saved(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(config_dir))
    repo = init_git_repo(tmp_path / "repo", branch="arya/profile-required")
    linear_sync.save_repo_linear_binding(team="Engineering", project="Codex Plugins", root=repo)

    missing_user = linear_sync.pre_tool_guard_decision({"tool_name": "apply_patch"}, root=repo)
    linear_sync.save_linear_user_profile(linear_name="Arya G")
    ready_for_kickoff = linear_sync.pre_tool_guard_decision({"tool_name": "apply_patch"}, root=repo)

    assert missing_user.blocked is True
    assert missing_user.message.startswith("LINEAR USER REQUIRED")
    assert "list Linear users" in missing_user.message
    assert "mcp__codex_apps__linear._list_users" in missing_user.message
    assert "mcp__linear.list_users" in missing_user.message
    assert "ask the human to choose their Linear user from that list" in missing_user.message
    assert "linear_start.py user-profile" in missing_user.message
    assert "linear_start.py configure-user" in missing_user.message
    assert "assign new Linear issues to that stored user" in missing_user.message
    assert ready_for_kickoff.blocked is True
    assert "Linear kickoff has not created active issue state" in ready_for_kickoff.message


@pytest.mark.parametrize(
    "tool",
    [
        "mcp__codex_apps__linear._save_issue",
        "mcp__codex_apps__linear._save_comment",
        "mcp__linear.save_issue",
        "mcp__linear.save_comment",
        "save_issue",
        "save_comment",
    ],
)
def test_pre_tool_guard_blocks_linear_writes_until_global_user_profile_is_saved(tmp_path, monkeypatch, tool):
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(config_dir))
    repo = init_git_repo(tmp_path / "repo", branch="arya/profile-required")
    linear_sync.save_repo_linear_binding(team="Engineering", project="Codex Plugins", root=repo)

    missing_user = linear_sync.pre_tool_guard_decision({"tool_name": tool}, root=repo)
    linear_sync.save_linear_user_profile(linear_name="Arya G")
    ready_payload = {"tool_name": tool}
    if tool.endswith("save_comment") or tool.endswith("_save_comment"):
        ready_payload["tool_input"] = {"body": "Kickoff started\n\nCodex bot: Arya G at 2026-07-03T18:42:10+00:00"}
    if tool.endswith("save_issue") or tool.endswith("_save_issue"):
        ready_payload["tool_input"] = {"id": "COR-123", "title": "Attach PR metadata"}
    ready = linear_sync.pre_tool_guard_decision(ready_payload, root=repo)

    assert missing_user.blocked is True
    assert missing_user.message.startswith("LINEAR USER REQUIRED")
    assert ready.blocked is False


@pytest.mark.parametrize(
    ("tool", "field"),
    [
        ("mcp__codex_apps__linear._save_comment", "body"),
        ("mcp__linear.save_comment", "comment"),
        ("save_comment", "content"),
        ("mcp__codex_apps__linear._save_issue", "description"),
        ("mcp__linear.save_issue", "body"),
        ("save_issue", "markdown"),
    ],
)
def test_pre_tool_guard_blocks_linear_content_without_attribution_footer(tmp_path, monkeypatch, tool, field):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    repo = init_git_repo(tmp_path / "repo", branch="arya/footer-required")
    linear_sync.save_linear_user_profile(linear_name="Arya G")
    tool_input = {field: "Kickoff started"}
    if tool.endswith("save_issue") or tool.endswith("_save_issue"):
        tool_input["id"] = "COR-123"

    decision = linear_sync.pre_tool_guard_decision(
        {"tool_name": tool, "tool_input": tool_input},
        root=repo,
    )

    assert decision.blocked is True
    assert decision.message.startswith("LINEAR ATTRIBUTION REQUIRED")
    assert "Codex bot: Arya G at <ISO-8601 UTC timestamp>" in decision.message


@pytest.mark.parametrize(
    "payload",
    [
        {
            "tool_name": "mcp__codex_apps__linear._save_comment",
            "tool_input": {"body": "Kickoff started\n\nCodex bot: Arya G at 2026-07-03T18:42:10+00:00"},
        },
        {
            "tool_name": "mcp__linear.save_issue",
            "arguments": {
                "id": "COR-123",
                "description": "Issue created\n\nCodex bot: Arya G at 2026-07-03T18:42:10+00:00",
            },
        },
        {
            "tool_name": "save_issue",
            "tool_input": {"id": "COR-123", "title": "Attach PR metadata"},
        },
    ],
)
def test_pre_tool_guard_allows_linear_writes_with_attribution_footer(tmp_path, monkeypatch, payload):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    repo = init_git_repo(tmp_path / "repo", branch="arya/footer-present")
    linear_sync.save_linear_user_profile(linear_name="Arya G")

    decision = linear_sync.pre_tool_guard_decision(payload, root=repo)

    assert decision.blocked is False


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "tool_name": "mcp__linear.save_issue",
                "tool_input": {"title": "Implement tracking", "team": "Engineering"},
            },
            "include an attributed description",
        ),
        (
            {
                "id": "tool-call-1",
                "tool_name": "mcp__linear.save_issue",
                "tool_input": {"title": "Implement tracking", "team": "Engineering"},
            },
            "include an attributed description",
        ),
        (
            {
                "tool_name": "mcp__linear.save_issue",
                "tool_input": {
                    "title": "Implement tracking",
                    "team": "Engineering",
                    "description": "Issue created\n\nCodex bot: Arya G at 2026-07-03T18:42:10+00:00",
                },
            },
            "assign the new Linear issue to Arya G",
        ),
        (
            {
                "tool_name": "mcp__linear.save_issue",
                "tool_input": {
                    "title": "Implement tracking",
                    "team": "Engineering",
                    "assignee": "Arya G",
                },
            },
            "include an attributed description",
        ),
    ],
)
def test_pre_tool_guard_blocks_issue_creation_without_assignee_and_attribution(tmp_path, monkeypatch, payload, expected):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    repo = init_git_repo(tmp_path / "repo", branch="arya/issue-create-required")
    linear_sync.save_linear_user_profile(linear_name="Arya G")

    decision = linear_sync.pre_tool_guard_decision(payload, root=repo)

    assert decision.blocked is True
    assert decision.message.startswith("LINEAR ISSUE CREATE REQUIRED")
    assert expected in decision.message


def test_pre_tool_guard_allows_issue_creation_with_assignee_and_attribution(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    repo = init_git_repo(tmp_path / "repo", branch="arya/issue-create-ready")
    linear_sync.save_linear_user_profile(linear_name="Arya G")

    decision = linear_sync.pre_tool_guard_decision(
        {
            "tool_name": "mcp__linear.save_issue",
            "tool_input": {
                "title": "Implement tracking",
                "team": "Engineering",
                "assignee": "Arya G",
                "description": "Issue created\n\nCodex bot: Arya G at 2026-07-03T18:42:10+00:00",
            },
        },
        root=repo,
    )

    assert decision.blocked is False


def test_pre_tool_guard_blocks_unbound_repo_writes_until_linear_destination_is_saved(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(config_dir))
    repo = init_git_repo(tmp_path / "repo", branch="arya/no-linear-binding")
    linear_sync.save_linear_user_profile(linear_name="Arya G")

    write_decision = linear_sync.pre_tool_guard_decision({"tool_name": "apply_patch"}, root=repo)
    branch_decision = linear_sync.pre_tool_guard_decision(
        {"tool_name": "Bash", "command": "git switch -c arya/normal-work"},
        root=repo,
    )

    assert write_decision.blocked is True
    assert branch_decision.blocked is True
    assert write_decision.message.startswith("LINEAR DESTINATION REQUIRED")
    assert "No Linear team/project is saved for this repo" in write_decision.message
    assert "LINEAR DESTINATION REQUIRED" in write_decision.message
    assert "Do not answer with a code patch" in write_decision.message
    assert "do not say you are blocked" in write_decision.message
    assert "Your next action must be to list Linear teams/projects" in write_decision.message
    assert "mcp__codex_apps__linear._list_teams" in write_decision.message
    assert "mcp__linear.list_projects" in write_decision.message
    assert "present the Linear project list" in write_decision.message
    assert "ask the human to choose the Linear project from that list" in write_decision.message
    assert "ask the user only which Linear team/project" in write_decision.message
    assert "This is the one required first-run human question" in write_decision.message
    assert "do not stop after listing projects" in write_decision.message
    assert "mcp__linear.save_issue" in write_decision.message
    assert "before creating the issue, branch, or PR" in write_decision.message
    assert "Do not ask the user for a Linear issue key" in write_decision.message
    assert "Create a new Linear issue from the user's implementation request" in write_decision.message
    assert branch_decision.message.startswith("LINEAR DESTINATION REQUIRED")
    assert "Create or reset implementation branches only after the saved Linear destination exists" in branch_decision.message
    assert "Do not answer with a code patch" in branch_decision.message
    assert "This is the one required first-run human question" in branch_decision.message
    assert "do not stop after listing projects" in branch_decision.message
    assert "linear_start.py kickoff" in branch_decision.message


def test_pre_tool_guard_allows_non_e3_origin_without_linear_binding(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(config_dir))
    repo = init_git_repo(
        tmp_path / "repo",
        branch="arya/external-work",
        origin_url="https://github.com/other-org/example.git",
    )
    linear_sync.save_linear_user_profile(linear_name="Arya G")

    write_decision = linear_sync.pre_tool_guard_decision({"tool_name": "apply_patch"}, root=repo)
    bash_decision = linear_sync.pre_tool_guard_decision(
        {"tool_name": "Bash", "command": "rm app.py"},
        root=repo,
    )
    branch_decision = linear_sync.pre_tool_guard_decision(
        {"tool_name": "Bash", "command": "git switch -c arya/normal-work"},
        root=repo,
    )
    status = linear_sync.repo_binding_status(root=repo)

    assert status["disabled"] is True
    assert status["scope_disabled"] is True
    assert status["configured"] is False
    assert status["binding"]["reason"] == "outside e3-solutions GitHub org"
    assert write_decision.blocked is False
    assert bash_decision.blocked is False
    assert branch_decision.blocked is False


def test_pre_tool_guard_allows_repo_without_origin(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(config_dir))
    repo = init_git_repo(tmp_path / "repo", branch="arya/no-origin", origin_url=None)
    linear_sync.save_linear_user_profile(linear_name="Arya G")

    write_decision = linear_sync.pre_tool_guard_decision({"tool_name": "apply_patch"}, root=repo)
    bash_decision = linear_sync.pre_tool_guard_decision(
        {"tool_name": "Bash", "command": "rm app.py"},
        root=repo,
    )
    branch_decision = linear_sync.pre_tool_guard_decision(
        {"tool_name": "Bash", "command": "git switch -c arya/normal-work"},
        root=repo,
    )
    status = linear_sync.repo_binding_status(root=repo)

    assert status["disabled"] is True
    assert status["scope_disabled"] is True
    assert status["configured"] is False
    assert write_decision.blocked is False
    assert bash_decision.blocked is False
    assert branch_decision.blocked is False


def test_pre_tool_guard_allows_non_git_directory_without_linear_binding(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(config_dir))
    workspace = tmp_path / "plain-workspace"
    workspace.mkdir()
    linear_sync.save_linear_user_profile(linear_name="Arya G")

    write_decision = linear_sync.pre_tool_guard_decision({"tool_name": "apply_patch"}, root=workspace)
    bash_decision = linear_sync.pre_tool_guard_decision(
        {"tool_name": "Bash", "command": "rm app.py"},
        root=workspace,
    )
    branch_decision = linear_sync.pre_tool_guard_decision(
        {"tool_name": "Bash", "command": "git switch -c arya/normal-work"},
        root=workspace,
    )
    status = linear_sync.repo_binding_status(root=workspace)

    assert status["disabled"] is True
    assert status["scope_disabled"] is True
    assert status["configured"] is False
    assert write_decision.blocked is False
    assert bash_decision.blocked is False
    assert branch_decision.blocked is False


def test_pre_tool_guard_blocks_e3_origin_without_linear_binding(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(config_dir))
    repo = init_git_repo(tmp_path / "repo", branch="arya/e3-work")
    linear_sync.save_linear_user_profile(linear_name="Arya G")

    write_decision = linear_sync.pre_tool_guard_decision({"tool_name": "apply_patch"}, root=repo)
    status = linear_sync.repo_binding_status(root=repo)

    assert status["disabled"] is False
    assert status["scope_disabled"] is False
    assert write_decision.blocked is True
    assert write_decision.message.startswith("LINEAR DESTINATION REQUIRED")


def test_pre_tool_guard_allows_opted_out_repo_without_user_or_active_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(config_dir))
    repo = init_git_repo(tmp_path / "repo", branch="arya/no-linear-sync")
    disabled = linear_sync.save_repo_linear_opt_out(reason="No Linear tracking for this project", root=repo)

    write_decision = linear_sync.pre_tool_guard_decision({"tool_name": "apply_patch"}, root=repo)
    branch_decision = linear_sync.pre_tool_guard_decision(
        {"tool_name": "Bash", "command": "git switch -c arya/normal-work"},
        root=repo,
    )
    status = linear_sync.repo_binding_status(root=repo)

    assert disabled["binding"]["disabled"] is True
    assert status["disabled"] is True
    assert status["configured"] is False
    assert status["binding"]["reason"] == "No Linear tracking for this project"
    assert write_decision.blocked is False
    assert branch_decision.blocked is False


def test_pre_tool_guard_allows_disable_linear_sync_before_user_profile(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(config_dir))
    repo = init_git_repo(tmp_path / "repo", branch="arya/no-linear-sync")
    command = (
        f"{sys.executable} {ROOT / 'plugins' / 'linear-progress-sync' / 'scripts' / 'linear_start.py'} "
        f"configure-repo --root {repo} --disable-linear-sync --reason 'No Linear tracking'"
    )

    disable_decision = linear_sync.pre_tool_guard_decision({"tool_name": "Bash", "command": command}, root=repo)
    normal_config_decision = linear_sync.pre_tool_guard_decision(
        {
            "tool_name": "Bash",
            "command": (
                f"{sys.executable} {ROOT / 'plugins' / 'linear-progress-sync' / 'scripts' / 'linear_start.py'} "
                f"configure-repo --root {repo} --team Engineering --project 'Codex Plugins'"
            ),
        },
        root=repo,
    )

    assert disable_decision.blocked is False
    assert normal_config_decision.blocked is True
    assert normal_config_decision.message.startswith("LINEAR USER REQUIRED")


def test_opted_out_repo_does_not_drain_or_prepare_linear_sync(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(config_dir))
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-123-opt-out")
    linear_sync.save_linear_user_profile(linear_name="Arya G")
    linear_sync.save_repo_linear_opt_out(reason="No Linear tracking", root=repo)
    queued = linear_sync.handle_post_tool_use({"tool_name": "apply_patch", "file_path": "app.py"}, root=repo)
    assert queued is None
    assert list((state_dir / "events").glob("*.json")) == []
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    stop_result = subprocess.run(
        [sys.executable, str(ROOT / "plugins" / "linear-progress-sync" / "scripts" / "stop_progress.py")],
        input="{}\n",
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert stop_result.returncode == 0
    assert stop_result.stdout == ""
    assert list((state_dir / "events").glob("*.json")) == []
    linear_sync.enqueue_event(
        "post_commit",
        {
            "id": "evt-opt-out",
            "type": "post_commit",
            "branch": "arya/cor-123-opt-out",
            "commit_sha": "abc123",
            "commit_subject": "COR-123 work that should not sync",
        },
        root=repo,
    )
    calls = []

    plan = linear_sync.foreground_sync_plan(root=repo)
    result = linear_sync.drain_once(
        root=repo,
        executor=lambda prompt, event, inference: calls.append((prompt, event, inference))
        or linear_sync.WorkerResult(True, "should not run"),
    )
    remaining_events = list((state_dir / "events").glob("*.json"))

    assert plan["eligible"] == []
    assert plan["held"] == []
    assert plan["skipped"][0]["reason"] == "Linear sync disabled for this repo"
    assert result["processed"] == 0
    assert result["skipped"] == 1
    assert result["failed"] == 0
    assert calls == []
    assert remaining_events == []


def test_stop_hook_is_noop_for_active_repo_with_uncommitted_changes(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(config_dir))
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-124-commit-only")
    linear_sync.save_linear_user_profile(linear_name="Arya G")
    linear_sync.write_active_issue(
        active_payload(repo, issue_key="COR-124", branch="arya/cor-124-commit-only", pr_number=124),
        root=repo,
    )
    (repo / "app.py").write_text("print('uncommitted')\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(ROOT / "plugins" / "linear-progress-sync" / "scripts" / "stop_progress.py")],
        input="{}\n",
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert list((state_dir / "events").glob("*.json")) == []


def test_pre_tool_guard_blocks_apply_patch_without_active_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(config_dir))
    repo = init_git_repo(tmp_path / "repo", branch="arya/no-linear-binding")
    linear_sync.save_linear_user_profile(linear_name="Arya G")

    decisions = [
        linear_sync.pre_tool_guard_decision({"tool_name": name}, root=repo)
        for name in (
            "apply_patch",
            "Edit",
            "MultiEdit",
            "Write",
        )
    ]

    assert all(decision.blocked for decision in decisions)
    assert all("No Linear team/project is saved for this repo" in decision.message for decision in decisions)


def test_handle_post_tool_use_does_not_queue_apply_patch_added_file(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(config_dir))
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-55-work")
    linear_sync.save_linear_user_profile(linear_name="Arya G")
    linear_sync.write_active_issue(
        {
            "issue_key": "COR-55",
            "issue_title": "Track new files",
            "issue_url": "https://linear.app/coreedge/issue/COR-55/track-new-files",
            "branch": "arya/cor-55-work",
            "repo": str(repo),
            "pr_url": "https://github.com/e3-solutions/codex-plugins/pull/55",
            "pr_number": 55,
            "linear_linked_at": "2026-07-01T00:00:00+00:00",
        },
        root=repo,
    )

    queued = linear_sync.handle_post_tool_use(
        {
            "tool_name": "apply_patch",
            "tool_input": {
                "command": """*** Begin Patch
*** Add File: src/new_module.py
+print("hello")
*** End Patch
""",
            },
        },
        root=repo,
    )

    assert queued is None
    assert list((state_dir / "events").glob("*.json")) == []


def test_handle_post_tool_use_queues_successful_git_commit(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-56-work")
    monkeypatch.setattr(
        linear_sync,
        "collect_commit_event",
        lambda *, root=None: {
            "commit_sha": "abc123",
            "short_sha": "abc123",
            "commit_subject": "COR-56 commit-only progress",
            "changed_files": ["src/app.py"],
            "branch": "arya/cor-56-work",
        },
    )
    drain_roots = []
    monkeypatch.setattr(linear_sync, "spawn_drain", lambda *, root=None: drain_roots.append(root))

    queued = linear_sync.handle_post_tool_use(
        {"tool_name": "Bash", "command": "git commit -m 'COR-56 work'", "exit_code": 0},
        root=repo,
    )

    assert queued is not None
    assert queued["type"] == "post_commit"
    assert queued["commit_sha"] == "abc123"
    assert drain_roots == [repo]


def test_pre_tool_guard_allows_unbound_repo_read_only_commands(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(config_dir))
    repo = init_git_repo(tmp_path / "repo", branch="arya/no-linear-binding")

    commands = [
        "git status --short",
        "stat README.md",
        "date",
        "pwd",
    ]

    for command in commands:
        decision = linear_sync.pre_tool_guard_decision(
            {"tool_name": "Bash", "command": command},
            root=repo,
        )
        assert decision.blocked is False, command


def test_pre_tool_guard_allows_unknown_non_write_bash_without_active_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
    bind_linear_repo(tmp_path, tmp_path, monkeypatch)
    commands = [
        "perl -e 'print qq(hi)'",
        "node -e 'console.log(\"hi\")'",
        "python -m pytest -q",
        "awk '{ print $1 }' app.py",
    ]

    for command in commands:
        decision = linear_sync.pre_tool_guard_decision(
            {"tool_name": "Bash", "command": command},
            root=tmp_path,
        )
        assert decision.blocked is False, command


def test_pre_tool_guard_blocks_write_like_bash_without_active_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    repo = init_git_repo(tmp_path / "repo")
    bind_linear_repo(repo, tmp_path, monkeypatch)
    linear_sync.save_linear_user_profile(linear_name="Arya G")
    commands = [
        "python - <<'PY'\nfrom pathlib import Path\nPath('app.py').write_text('hi')\nPY",
        "sed -i '' 's/a/b/' app.py",
        "rm app.py",
    ]

    for command in commands:
        decision = linear_sync.pre_tool_guard_decision(
            {"tool_name": "Bash", "command": command},
            root=repo,
        )
        assert decision.blocked is True, command
        assert "appears to write files or mutate repo state" in decision.message


def test_pre_tool_guard_blocks_unbound_repo_without_active_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(config_dir))
    repo = init_git_repo(tmp_path / "repo", branch="arya/no-linear-binding")
    linear_sync.save_linear_user_profile(linear_name="Arya G")

    write_decision = linear_sync.pre_tool_guard_decision({"tool_name": "apply_patch"}, root=repo)
    branch_decision = linear_sync.pre_tool_guard_decision(
        {"tool_name": "Bash", "command": "git switch -c arya/normal-work"},
        root=repo,
    )

    assert write_decision.blocked is True
    assert branch_decision.blocked is True
    assert write_decision.message.startswith("LINEAR DESTINATION REQUIRED")
    assert "No Linear team/project is saved for this repo" in write_decision.message
    assert branch_decision.message.startswith("LINEAR DESTINATION REQUIRED")


def test_pre_tool_guard_blocks_writes_with_incomplete_active_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    save_linear_user(tmp_path, monkeypatch)
    repo = init_git_repo(tmp_path / "repo")
    linear_sync.write_json_atomic(
        linear_sync.active_issue_path(root=repo),
        {"issue_key": "COR-40"},
    )

    decision = linear_sync.pre_tool_guard_decision({"tool_name": "apply_patch"}, root=repo)

    assert decision.blocked is True
    assert "missing repo" in decision.message


def test_pre_tool_guard_blocks_active_state_without_pr_evidence(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    save_linear_user(tmp_path, monkeypatch)
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-40-work")
    incomplete = active_payload(repo, issue_key="COR-40", issue_title="Missing PR evidence", pr_number=40)
    incomplete.pop("pr_url")
    incomplete.pop("pr_number")
    linear_sync.write_json_atomic(
        linear_sync.active_issue_path(root=repo),
        incomplete,
    )

    decision = linear_sync.pre_tool_guard_decision({"tool_name": "apply_patch"}, root=repo)

    assert decision.blocked is True
    assert "missing pr_url" in decision.message


def test_pre_tool_guard_blocks_when_current_branch_cannot_be_verified(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    save_linear_user(tmp_path, monkeypatch)
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-41-work")
    subprocess.run(["git", "switch", "--detach"], cwd=repo, check=True)
    linear_sync.write_active_issue(
        {
            "issue_key": "COR-41",
            "issue_title": "Detached branch",
            "issue_url": "https://linear.app/coreedge/issue/COR-41/detached-branch",
            "branch": "arya/cor-41-work",
            "repo": str(repo),
            "pr_url": "https://github.com/e3-solutions/codex-plugins/pull/41",
            "pr_number": 41,
            "linear_linked_at": "2026-07-01T00:00:00+00:00",
        },
        root=repo,
    )

    decision = linear_sync.pre_tool_guard_decision({"tool_name": "apply_patch"}, root=repo)

    assert decision.blocked is True
    assert "current branch could not be verified" in decision.message


def test_pre_tool_guard_blocks_long_form_branch_creation_without_active_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    repo = init_git_repo(tmp_path / "repo")
    bind_linear_repo(repo, tmp_path, monkeypatch)

    commands = [
        "git switch --create arya/new-work",
        "git status --short\ngit switch -c arya/new-work",
        "git status --short\ngit checkout -b arya/new-work",
        "bash -lc 'git switch -c arya/new-work'",
        "git checkout --branch arya/new-work",
        "git checkout -B arya/new-work",
        "git switch --orphan arya/new-work",
        "git switch --track origin/arya/new-work",
        "git checkout --track origin/arya/new-work",
        "git branch --track arya/new-work origin/main",
        "git branch -f arya/new-work origin/main",
        "git branch --force arya/new-work origin/main",
        "git branch --create-reflog arya/new-work origin/main",
        "git branch --no-track arya/new-work HEAD",
        "git branch --quiet arya/new-work HEAD",
        "git branch -q arya/new-work HEAD",
        "git worktree add ../new-worktree",
        "git worktree add --orphan arya/new-work ../new-worktree",
    ]

    for command in commands:
        decision = linear_sync.pre_tool_guard_decision(
            {"tool_name": "Bash", "command": command},
            root=repo,
        )
        assert decision.blocked is True, command


def test_pre_tool_guard_blocks_unsafe_bash_writes_without_active_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    repo = init_git_repo(tmp_path / "repo")
    bind_linear_repo(repo, tmp_path, monkeypatch)
    commands = [
        "touch app.py",
        "sed -i '' 's/a/b/' app.py",
        "perl -0pi -e 's/a/b/' app.py",
        "perl -pi -e 's/a/b/' app.py",
        "ruby -pi -e 'gsub(/a/, \"b\")' app.py",
        "python3 -c 'open(\"app.py\", \"w\").write(\"x\")'",
        "cat > app.py",
        "echo hi > app.py",
        "echo hi>app.py",
        "cat README.md>copy.md",
        "rg foo>out",
        "echo hi &>out.log",
        "python3 plugins/linear-progress-sync/scripts/linear_start.py repo-binding --root . > app.py",
        "/linear-start > app.py",
    ]

    for command in commands:
        decision = linear_sync.pre_tool_guard_decision(
            {"tool_name": "Bash", "command": command},
            root=repo,
        )
        assert decision.blocked is True, command
        assert "Linear kickoff" in decision.message


def test_pre_tool_guard_blocks_shell_substitutions_without_active_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    repo = init_git_repo(tmp_path / "repo")
    bind_linear_repo(repo, tmp_path, monkeypatch)
    commands = [
        "git status $(touch app.py)",
        "git status `touch app.py`",
        "cat <(touch app.py)",
        'echo "don\'t $(touch app.py)"',
    ]

    for command in commands:
        decision = linear_sync.pre_tool_guard_decision(
            {"tool_name": "Bash", "command": command},
            root=repo,
        )
        assert decision.blocked is True, command


def test_pre_tool_guard_allows_general_bash_with_active_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    save_linear_user(tmp_path, monkeypatch)
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-49-active")
    linear_sync.write_active_issue(
        active_payload(repo, issue_key="COR-49", issue_title="Active Bash work", pr_number=49),
        root=repo,
    )
    commands = [
        "python3 -c 'open(\"app.py\", \"w\").write(\"x\")'",
        "bash -c 'echo hi > app.py'",
        "cmd='echo hi > app.py'; eval \"$cmd\"",
        "cat README.md > copy.md",
    ]

    for command in commands:
        decision = linear_sync.pre_tool_guard_decision(
            {"tool_name": "Bash", "command": command},
            root=repo,
        )
        assert decision.blocked is False, command


def test_pre_tool_guard_blocks_update_ref_branch_creation_with_active_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    save_linear_user(tmp_path, monkeypatch)
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-56-active")
    linear_sync.write_active_issue(
        active_payload(repo, issue_key="COR-56", issue_title="Active update-ref guard", pr_number=56),
        root=repo,
    )

    commands = [
        "git update-ref refs/heads/arya/cor-57-bypass HEAD",
        'printf "create refs/heads/arya/cor-57-bypass HEAD\\n" | git update-ref --stdin',
        "git update-ref --stdin < /tmp/refs.txt",
    ]

    for command in commands:
        decision = linear_sync.pre_tool_guard_decision(
            {"tool_name": "Bash", "command": command},
            root=repo,
        )
        assert decision.blocked is True, command
        assert "only through the Linear kickoff workflow" in decision.message


def test_pre_tool_guard_blocks_ref_writing_git_commands_with_active_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    save_linear_user(tmp_path, monkeypatch)
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-62-active")
    linear_sync.write_active_issue(
        active_payload(repo, issue_key="COR-62", issue_title="Active ref guard", pr_number=62),
        root=repo,
    )
    commands = [
        "/usr/bin/git switch -c arya/cor-63-bypass",
        "/usr/bin/git update-ref refs/heads/arya/cor-63-bypass HEAD",
        "git symbolic-ref refs/heads/arya/cor-63-bypass HEAD",
        "git stash branch arya/cor-63-bypass",
    ]

    for command in commands:
        decision = linear_sync.pre_tool_guard_decision(
            {"tool_name": "Bash", "command": command},
            root=repo,
        )
        assert decision.blocked is True, command
        assert "only through the Linear kickoff workflow" in decision.message


def test_pre_tool_guard_blocks_env_split_string_branch_creation_with_active_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    save_linear_user(tmp_path, monkeypatch)
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-64-active")
    linear_sync.write_active_issue(
        active_payload(repo, issue_key="COR-64", issue_title="Active env split guard", pr_number=64),
        root=repo,
    )

    decision = linear_sync.pre_tool_guard_decision(
        {"tool_name": "Bash", "command": 'env -S "git switch -c arya/cor-65-bypass"'},
        root=repo,
    )

    assert decision.blocked is True
    assert "only through the Linear kickoff workflow" in decision.message


def test_pre_tool_guard_blocks_switch_and_checkout_with_active_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    save_linear_user(tmp_path, monkeypatch)
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-58-active")
    linear_sync.write_active_issue(
        active_payload(repo, issue_key="COR-58", issue_title="Active switch guard", pr_number=58),
        root=repo,
    )
    commands = [
        "git switch arya/cor-59-bypass",
        "git checkout arya/cor-59-bypass",
    ]

    for command in commands:
        decision = linear_sync.pre_tool_guard_decision(
            {"tool_name": "Bash", "command": command},
            root=repo,
        )
        assert decision.blocked is True, command
        assert "only through the Linear kickoff workflow" in decision.message


def test_pre_tool_guard_blocks_background_write_segments_without_active_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    repo = init_git_repo(tmp_path / "repo")
    bind_linear_repo(repo, tmp_path, monkeypatch)
    commands = [
        "git status & touch app.py",
        "rg foo README.md & touch app.py",
        "python3 plugins/linear-progress-sync/scripts/linear_start.py repo-binding --root . & touch app.py",
    ]

    for command in commands:
        decision = linear_sync.pre_tool_guard_decision(
            {"tool_name": "Bash", "command": command},
            root=repo,
        )
        assert decision.blocked is True, command


def test_pre_tool_guard_allows_quoted_redirection_text_as_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))

    commands = [
        "rg '->' README.md",
        'rg "foo|bar" README.md',
        'grep "foo;bar" README.md',
        'grep "(foo)" README.md',
        'grep "{foo}" README.md',
        "rg bash -c README.md",
        "rg git switch -c README.md",
        "echo git switch -c",
        "rg eval README.md",
        "rg GIT_CONFIG_COUNT=1 README.md",
        "echo GIT_CONFIG_COUNT=1",
        "git grep GIT_CONFIG_COUNT=1",
        "rg env -S README.md",
        "echo env -S",
        "find . -name source -print",
        "find . -name bash -print",
        "git grep source",
        "git grep eval",
        "/usr/bin/git status --short",
        'git log --pretty=format:"%h|%s"',
    ]

    for command in commands:
        decision = linear_sync.pre_tool_guard_decision(
            {"tool_name": "Bash", "command": command},
            root=tmp_path,
        )
        assert decision.blocked is False, command


def test_pre_tool_guard_blocks_branch_creation_even_with_active_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    save_linear_user(tmp_path, monkeypatch)
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-47-active")
    linear_sync.write_active_issue(active_payload(repo, issue_key="COR-47", issue_title="Active", pr_number=47), root=repo)

    decision = linear_sync.pre_tool_guard_decision(
        {"tool_name": "Bash", "command": "git switch -c arya/cor-48-other-work"},
        root=repo,
    )

    assert decision.blocked is True
    assert "only through the Linear kickoff workflow" in decision.message


def test_pre_tool_guard_blocks_chained_branch_creation_after_kickoff_helper(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    repo = init_git_repo(tmp_path / "repo")
    bind_linear_repo(repo, tmp_path, monkeypatch)
    commands = [
        "python3 plugins/linear-progress-sync/scripts/linear_start.py repo-binding --root . && git switch -c arya/bypass",
        "echo linear_start.py; git switch -c arya/bypass",
        "/linear-start && git checkout -B arya/bypass",
    ]

    for command in commands:
        decision = linear_sync.pre_tool_guard_decision(
            {"tool_name": "Bash", "command": command},
            root=repo,
        )
        assert decision.blocked is True, command


def test_pre_tool_guard_blocks_kickoff_until_global_linear_user_profile_is_saved(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    repo = init_git_repo(tmp_path / "repo")
    linear_sync.save_repo_linear_binding(team="Engineering", project="Codex Plugins", root=repo)

    read_decision = linear_sync.pre_tool_guard_decision(
        {"tool_name": "Bash", "command": "git status --short"},
        root=repo,
    )
    missing_user_decision = linear_sync.pre_tool_guard_decision(
        {"tool_name": "Bash", "command": "python3 plugins/linear-progress-sync/scripts/linear_start.py kickoff"},
        root=repo,
    )
    profile_decision = linear_sync.pre_tool_guard_decision(
        {
            "tool_name": "Bash",
            "command": (
                "python3 plugins/linear-progress-sync/scripts/linear_start.py "
                "configure-user --linear-name 'Arya G'"
            ),
        },
        root=repo,
    )
    linear_sync.save_linear_user_profile(linear_name="Arya G")
    ready_decision = linear_sync.pre_tool_guard_decision(
        {"tool_name": "Bash", "command": "python3 plugins/linear-progress-sync/scripts/linear_start.py kickoff"},
        root=repo,
    )

    assert read_decision.blocked is False
    assert missing_user_decision.blocked is True
    assert "LINEAR USER REQUIRED" in missing_user_decision.message
    assert profile_decision.blocked is False
    assert ready_decision.blocked is False


def test_enqueue_event_carries_active_issue_key(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-38-carry-active-issue")
    linear_sync.write_active_issue(
        active_payload(repo, issue_key="COR-38", issue_title="Carry active issue", pr_number=38),
        root=repo,
    )

    event = linear_sync.enqueue_event(
        "post_commit",
        {"commit_sha": "abc123", "commit_subject": "COR-38 commit-only progress"},
        root=repo,
    )

    assert event["issue_key"] == "COR-38"


def test_linear_progress_hooks_do_not_register_stop_event():
    for relative_path in (
        "plugins/linear-progress-sync/hooks.json",
        "plugins/linear-progress-sync/hooks/hooks.json",
    ):
        config = json.loads((ROOT / relative_path).read_text(encoding="utf-8"))
        assert "Stop" not in config["hooks"]


def test_linear_start_dry_run_plan_contains_git_and_gh_commands(tmp_path):
    plan = linear_sync.linear_start_plan(
        issue_key="COR-39",
        issue_title="Start with Linear",
        issue_url="https://linear.app/coreedge/issue/COR-39/start-with-linear",
        branch="arya/cor-39-start-with-linear",
        team="Engineering",
        project="Codex Plugins",
        root=tmp_path,
    )

    commands = "\n".join(plan["commands"])
    assert "git switch" in commands
    assert "git commit --allow-empty -m 'chore: start COR-39'" in commands
    assert "git push -u origin arya/cor-39-start-with-linear" in commands
    assert "gh pr create --draft" in commands
    assert plan["active_state"]["issue_key"] == "COR-39"
    assert plan["active_state"]["team"] == "Engineering"
    assert plan["active_state"]["project"] == "Codex Plugins"


def test_linear_start_requires_issue_url_before_side_effects(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    linear_sync.save_linear_user_profile(linear_name="Arya G")
    calls = []
    monkeypatch.setattr(
        linear_sync,
        "run_git",
        lambda args, root=None: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )

    with pytest.raises(ValueError, match="issue_url"):
        linear_sync.run_linear_start(
            issue_key="COR-41",
            issue_title="Require issue URL",
            issue_url=None,
            branch="arya/cor-41-require-url",
            root=tmp_path,
        )

    assert calls == []


def test_linear_start_requires_user_profile_before_side_effects(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-41-original")
    calls = []
    monkeypatch.setattr(
        linear_sync,
        "run_git",
        lambda args, root=None: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )
    monkeypatch.setattr(
        linear_sync,
        "run_local_command",
        lambda args, root=None: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )

    with pytest.raises(ValueError, match="Linear user profile"):
        linear_sync.run_linear_start(
            issue_key="COR-41",
            issue_title="Require user profile",
            issue_url="https://linear.app/coreedge/issue/COR-41/require-user-profile",
            branch="arya/cor-41-require-user-profile",
            root=repo,
        )

    assert calls == []
    current = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()
    branch_created = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", "refs/heads/arya/cor-41-require-user-profile"],
        cwd=repo,
    )
    assert current == "arya/cor-41-original"
    assert branch_created.returncode != 0


def test_linear_start_returns_pending_state_without_writing_active_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    linear_sync.save_linear_user_profile(linear_name="Arya G")
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-42-work")
    calls = []

    monkeypatch.setattr(
        linear_sync,
        "run_git",
        lambda args, root=None: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )
    monkeypatch.setattr(
        linear_sync,
        "run_local_command",
        lambda args, root=None: subprocess.CompletedProcess(
            args,
            0,
            "https://github.com/e3-solutions/codex-plugins/pull/42\n",
            "",
        ),
    )

    result = linear_sync.run_linear_start(
        issue_key="COR-42",
        issue_title="Pending until linked",
        issue_url="https://linear.app/coreedge/issue/COR-42/pending-until-linked",
        branch="arya/cor-42-work",
        root=repo,
    )

    assert result["pending_active_state"]["pr_number"] == 42
    assert "linear_start.py activate" in result["activation_command"]
    assert linear_sync.read_active_issue(root=repo) is None


def test_activate_linear_start_writes_linked_active_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    linear_sync.save_linear_user_profile(linear_name="Arya G")
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-43-activate")

    active = linear_sync.activate_linear_start(
        issue_key="COR-43",
        issue_title="Activate after Linear link",
        issue_url="https://linear.app/coreedge/issue/COR-43/activate-after-linear-link",
        branch="arya/cor-43-activate",
        pr_url="https://github.com/e3-solutions/codex-plugins/pull/43",
        pr_number=43,
        team="Engineering",
        project="Codex Plugins",
        root=repo,
        linked_at="2026-07-01T00:00:00+00:00",
    )

    assert active["linear_linked_at"] == "2026-07-01T00:00:00+00:00"
    assert linear_sync.read_current_active_issue(root=repo)["issue_key"] == "COR-43"


def test_linear_start_requires_parseable_pr_url_before_writing_active_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    linear_sync.save_linear_user_profile(linear_name="Arya G")
    monkeypatch.setattr(linear_sync, "current_branch", lambda root=None: "arya/cor-42-work")
    monkeypatch.setattr(
        linear_sync,
        "run_git",
        lambda args, root=None: subprocess.CompletedProcess(args, 0, "", ""),
    )
    monkeypatch.setattr(
        linear_sync,
        "run_local_command",
        lambda args, root=None: subprocess.CompletedProcess(args, 0, "Created draft PR", ""),
    )

    with pytest.raises(RuntimeError, match="pull request URL"):
        linear_sync.run_linear_start(
            issue_key="COR-42",
            issue_title="Require PR URL",
            issue_url="https://linear.app/coreedge/issue/COR-42/require-pr-url",
            branch="arya/cor-42-work",
            root=tmp_path,
        )

    assert linear_sync.read_active_issue(root=tmp_path) is None


@pytest.mark.parametrize("change_kind", ("staged", "staged_tmp", "unstaged", "untracked"))
def test_linear_start_requires_clean_worktree_before_kickoff(tmp_path, monkeypatch, change_kind):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    linear_sync.save_linear_user_profile(linear_name="Arya G")
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-43-original")
    if change_kind == "staged":
        (repo / "staged.txt").write_text("staged content\n", encoding="utf-8")
        subprocess.run(["git", "add", "staged.txt"], cwd=repo, check=True)
    elif change_kind == "staged_tmp":
        (repo / "accidental.tmp").write_text("staged temp content\n", encoding="utf-8")
        subprocess.run(["git", "add", "accidental.tmp"], cwd=repo, check=True)
    elif change_kind == "unstaged":
        (repo / "tracked.txt").write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-m", "add tracked"], cwd=repo, check=True)
        (repo / "tracked.txt").write_text("after\n", encoding="utf-8")
    else:
        (repo / "untracked.txt").write_text("untracked content\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="clean worktree"):
        linear_sync.run_linear_start(
            issue_key="COR-43",
            issue_title="Reject dirty kickoff",
            issue_url="https://linear.app/coreedge/issue/COR-43/reject-dirty-kickoff",
            branch="arya/cor-43-reject-dirty-kickoff",
            root=repo,
        )

    assert linear_sync.current_branch(repo) == "arya/cor-43-original"
    assert linear_sync.local_branch_exists("arya/cor-43-reject-dirty-kickoff", root=repo) is False


def test_clean_worktree_reports_staged_ignored_suffix_files(tmp_path, monkeypatch):
    monkeypatch.delenv("LINEAR_SYNC_STATE_DIR", raising=False)
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-46-work")
    (repo / "accidental.tmp").write_text("staged temp content\n", encoding="utf-8")
    subprocess.run(["git", "add", "accidental.tmp"], cwd=repo, check=True)

    entries = linear_sync.worktree_status_entries(root=repo)

    assert any("accidental.tmp" in entry for entry in entries)


def test_clean_worktree_ignores_plugin_owned_state_in_fresh_repo(tmp_path, monkeypatch):
    monkeypatch.delenv("LINEAR_SYNC_STATE_DIR", raising=False)
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-44-work")
    linear_sync.ensure_state(repo)

    assert linear_sync.worktree_status_entries(root=repo) == []


def test_clean_worktree_still_reports_unrelated_codex_files(tmp_path, monkeypatch):
    monkeypatch.delenv("LINEAR_SYNC_STATE_DIR", raising=False)
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-45-work")
    linear_sync.ensure_state(repo)
    other = repo / ".codex" / "other.json"
    other.write_text("{}\n", encoding="utf-8")

    entries = linear_sync.worktree_status_entries(root=repo)

    assert any(".codex/other.json" in entry for entry in entries)


def test_clean_worktree_status_parser_handles_renames_and_quoted_paths():
    assert linear_sync.status_entry_blocks_kickoff('R  ".codex/linear-sync/old state.json" -> "src/app file.py"')
    assert linear_sync.status_entry_blocks_kickoff('R  "src/app file.py" -> ".codex/linear-sync/new state.json"')
    assert linear_sync.status_entry_blocks_kickoff('A  "src/app file.py"')
    assert not linear_sync.status_entry_blocks_kickoff(
        'R  ".codex/linear-sync/old state.json" -> ".codex/linear-sync/new state.json"'
    )


def test_repo_identity_normalizes_github_origin(tmp_path):
    repo = init_git_repo(tmp_path / "repo")

    assert linear_sync.repo_identity(repo) == "e3-solutions/codex-plugins"


def test_repo_linear_binding_round_trips_by_repo_identity(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(config_dir))
    repo = init_git_repo(tmp_path / "repo", origin_url="https://github.com/e3-solutions/codex-plugins.git")

    missing = linear_sync.repo_binding_status(root=repo)
    saved = linear_sync.save_repo_linear_binding(
        team="Engineering",
        project="Codex Plugins",
        root=repo,
    )
    loaded = linear_sync.repo_binding_status(root=repo)

    assert missing["configured"] is False
    assert saved["repo"] == "e3-solutions/codex-plugins"
    assert loaded["configured"] is True
    assert loaded["binding"] == {"team": "Engineering", "project": "Codex Plugins"}
    assert "e3-solutions/codex-plugins" in (config_dir / "repos.json").read_text(encoding="utf-8")


def test_linear_start_configure_repo_can_disable_linear_sync(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(config_dir))
    repo = init_git_repo(tmp_path / "repo")

    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "plugins" / "linear-progress-sync" / "scripts" / "linear_start.py"),
            "configure-repo",
            "--root",
            str(repo),
            "--disable-linear-sync",
            "--reason",
            "No Linear tracking",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    status = linear_sync.repo_binding_status(root=repo)

    assert result.returncode == 0
    assert status["disabled"] is True
    assert status["binding"] == {"disabled": True, "reason": "No Linear tracking"}


def test_setup_plan_is_global_by_default_and_does_not_install_repo_hook(tmp_path):
    plan = linear_sync.setup_plan(plugin_repo_root=ROOT, target_repo_root=tmp_path)
    commands = "\n".join(plan["commands"])
    notes = "\n".join(plan["notes"])

    assert "gh auth status" in commands
    assert "codex plugin marketplace add" in commands
    assert "codex plugin add linear-progress-sync@coreedge-local" in commands
    assert "codex plugin add codex-session-logging@coreedge-local" in commands
    assert "install_codex_hooks.py" in commands
    assert "codex mcp add linear --url https://mcp.linear.app/mcp" in commands
    assert "codex mcp login linear" not in commands
    assert "codex mcp login linear after setup" in notes
    assert "review hooks" in notes
    assert "Codex Session Logging hooks" in notes
    assert "First use lists Linear users" in notes
    assert "First use in a repo lists Linear teams/projects" in notes
    assert "--disable-linear-sync" in notes
    assert "SessionStart" in notes
    assert "LINEAR_SYNC_AUTO_UPDATE=0" in notes
    assert "write-like Bash commands" in notes
    assert "branch creation wait for active Linear state" in notes
    assert "install_git_hook.py" not in commands
    assert plan["per_repo_setup_required"] is False


def test_setup_plan_can_include_optional_repo_git_hook(tmp_path):
    plan = linear_sync.setup_plan(
        plugin_repo_root=ROOT,
        target_repo_root=tmp_path,
        with_git_hook=True,
    )
    commands = "\n".join(plan["commands"])

    assert "install_git_hook.py" in commands
    assert str(tmp_path) in commands


def test_setup_script_exists_and_exposes_dry_run():
    script = ROOT / "plugins/linear-progress-sync/scripts/setup.py"
    hook_script = ROOT / "plugins/linear-progress-sync/scripts/install_codex_hooks.py"
    assert script.exists()
    assert hook_script.exists()
    text = script.read_text(encoding="utf-8")
    assert "--dry-run" in text
    assert "--with-git-hook" in text
    assert "codex mcp login linear" in text
    assert "Before Linear kickoff, file edits, write-like Bash commands, and branch creation wait" in text
    assert "No per-repo setup is needed" in text


def test_setup_summary_prints_team_next_steps(capsys):
    linear_setup.print_summary(
        {
            "ok": True,
            "results": [
                {"command": "gh auth status", "ok": True, "message": ""},
            ],
        }
    )
    output = capsys.readouterr().out

    assert "Run: codex mcp login linear" in output
    assert "trust the Linear Progress Sync and Codex Session Logging hooks once" in output
    assert "list Linear users/projects" in output
    assert "--disable-linear-sync" in output
    assert "LINEAR_SYNC_AUTO_UPDATE=0" in output
    assert "Before Linear kickoff, file edits, write-like Bash commands, and branch creation wait" in output
    assert "No per-repo setup is needed" in output


def test_update_plugin_installs_newer_manifest_archive(tmp_path, monkeypatch):
    update_plugin = load_update_plugin()
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    cache_parent = tmp_path / "cache" / "coreedge-local" / "linear-progress-sync"
    current = write_minimal_plugin(cache_parent / "0.2.0", version="0.2.0")
    archive, digest = make_plugin_archive(tmp_path, version="0.2.1")
    manifest = tmp_path / "latest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": "0.2.1",
                "archive_url": archive.as_uri(),
                "sha256": digest,
                "plugin_subdir": "plugins/linear-progress-sync",
            }
        ),
        encoding="utf-8",
    )

    result = update_plugin.run_update(
        current_plugin_root=current,
        cache_parent=cache_parent,
        manifest_url=manifest.as_uri(),
        state_path=tmp_path / "update-state.json",
        force=True,
        install_hooks=False,
    )

    installed_manifest = json.loads((cache_parent / "0.2.1" / ".codex-plugin" / "plugin.json").read_text())
    assert result["updated"] is True
    assert result["installed_version"] == "0.2.1"
    assert installed_manifest["version"] == "0.2.1"


def test_update_plugin_syncs_default_marketplace_plugins_and_hooks_when_bootstrap_current(tmp_path, monkeypatch):
    update_plugin = load_update_plugin()
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    hooks_path = codex_home / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "/tmp/notify.sh"}]},
                        {
                            "hooks": [{
                                "type": "command",
                                "command": "python3 ~/.codex/plugins/cache/coreedge-local/linear-progress-sync/0.2.2/scripts/linear_sync.py",
                            }]
                        },
                        {
                            "hooks": [{
                                "type": "command",
                                "command": "python3 ~/.codex/plugins/cache/coreedge-local/codex-session-logging/0.1.0/scripts/session_start.py",
                            }]
                        },
                    ],
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    cache_root = tmp_path / "cache" / "coreedge-local"
    cache_parent = cache_root / "linear-progress-sync"
    current = write_minimal_plugin(
        cache_parent,
        directory_name="0.2.2",
        version="0.2.2",
        hook_events=("SessionStart", "PreToolUse"),
    )
    archive, digest = make_marketplace_archive(tmp_path, bootstrap_version="0.2.2")
    manifest = tmp_path / "latest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": "0.2.2",
                "archive_url": archive.as_uri(),
                "sha256": digest,
                "plugin_subdir": "plugins/linear-progress-sync",
            }
        ),
        encoding="utf-8",
    )

    result = update_plugin.run_update(
        current_plugin_root=current,
        cache_parent=cache_parent,
        manifest_url=manifest.as_uri(),
        state_path=tmp_path / "update-state.json",
        force=True,
    )
    second = update_plugin.run_update(
        current_plugin_root=current,
        cache_parent=cache_parent,
        manifest_url=manifest.as_uri(),
        state_path=tmp_path / "update-state.json",
        force=True,
    )

    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    hooks_text = json.dumps(hooks, sort_keys=True)
    assert result["updated"] is True
    assert {plugin["name"] for plugin in result["installed_plugins"]} == {"codex-session-logging"}
    assert second["updated"] is False
    assert second["installed_plugins"] == []
    assert (cache_root / "codex-session-logging" / "0.1.0" / ".codex-plugin" / "plugin.json").exists()
    assert not (cache_root / "internal-experiment").exists()
    assert "/tmp/notify.sh" in hooks_text
    assert "linear-progress-sync" not in hooks_text
    assert "codex-session-logging" not in hooks_text
    assert "internal-experiment" not in hooks_text
    assert {plugin["registration"] for plugin in result["hooks"]} == {"plugin-native"}


def test_legacy_upgrade_installs_presence_on_next_automatic_resident_cycle(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    cache_parent = codex_home / "plugins" / "cache" / "coreedge-local" / "linear-progress-sync"
    write_minimal_plugin(cache_parent, directory_name="0.3.0", version="0.3.0")
    repo_root = tmp_path / "payload" / "codex-plugins-main"
    shutil.copytree(ROOT / ".agents", repo_root / ".agents")
    for name in ("linear-progress-sync", "codex-session-logging"):
        shutil.copytree(ROOT / "plugins" / name, repo_root / "plugins" / name)
    archive = tmp_path / "marketplace.zip"
    with zipfile.ZipFile(archive, "w") as zip_file:
        for path in repo_root.rglob("*"):
            zip_file.write(path, path.relative_to(tmp_path / "payload"))
    manifest = tmp_path / "latest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": "0.3.2",
                "archive_url": archive.as_uri(),
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "plugin_subdir": "plugins/linear-progress-sync",
            }
        ),
        encoding="utf-8",
    )
    legacy_runtime = tmp_path / "legacy-runtime"
    legacy_runtime.mkdir()
    for name in ("linear_sync.py", "resident_updater.py", "update_plugin.py"):
        source = subprocess.run(
            ["git", "show", f"d0e6f65:{'plugins/linear-progress-sync/scripts'}/{name}"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout
        (legacy_runtime / name).write_text(source, encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_launchctl = fake_bin / "launchctl"
    fake_launchctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_launchctl.chmod(0o755)
    home = tmp_path / "home"
    environment = {
        **os.environ,
        "HOME": str(home),
        "CODEX_HOME": str(codex_home),
        "LINEAR_SYNC_CONFIG_DIR": str(tmp_path / "config"),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
    common_args = [
        "--cache-parent",
        str(cache_parent),
        "--manifest-url",
        manifest.as_uri(),
        "--state-path",
        str(tmp_path / "update.json"),
        "--force",
        "--resident",
        "--json",
    ]

    first_cycle = subprocess.run(
        [sys.executable, str(legacy_runtime / "update_plugin.py"), "--plugin-root", str(cache_parent / "0.3.0"), *common_args],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    resident_root = codex_home / "coreedge"

    assert first_cycle.returncode == 0, first_cycle.stderr
    assert json.loads(first_cycle.stdout)["resident"]["version"] == "0.3.2"
    assert (resident_root / "runtime" / "current").resolve().name == "0.3.2"
    assert not (home / "Library" / "LaunchAgents" / "com.coreedge.codex-session-presence.plist").exists()

    second_cycle = subprocess.run(
        [
            sys.executable,
            str(resident_root / "runtime" / "current" / "update_plugin.py"),
            "--plugin-root",
            str(resident_root / "marketplace" / "current" / "plugins" / "linear-progress-sync"),
            *common_args,
        ],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    result = json.loads(second_cycle.stdout)

    assert second_cycle.returncode == 0, second_cycle.stderr
    assert result["updated"] is True
    assert result["resident"]["service"]["presence"]["scheduled"] is True
    assert (resident_root / "presence.sh").is_file()
    assert (home / "Library" / "LaunchAgents" / "com.coreedge.codex-session-presence.plist").is_file()


def test_legacy_updater_merge_requires_followup_native_cleanup():
    update_plugin = load_update_plugin()
    plugin_config = linear_sync.read_plugin_hooks_config(ROOT, "codex-session-logging")
    existing = {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "/tmp/user-hook.sh"}]},
            ]
        }
    }

    first_session = update_plugin.merge_plugin_hooks(
        existing,
        name="codex-session-logging",
        plugin_config=plugin_config,
    )
    following_session = linear_sync.remove_plugin_hooks(
        first_session,
        plugin_name="codex-session-logging",
        plugin_config=plugin_config,
    )

    assert "codex-session-logging" in json.dumps(first_session)
    assert "codex-session-logging" not in json.dumps(following_session)
    assert "/tmp/user-hook.sh" in json.dumps(following_session)


def test_install_plugin_refreshes_installed_file_mtimes_for_hook_selection(tmp_path):
    update_plugin = load_update_plugin()
    source = write_minimal_plugin(
        tmp_path / "source",
        name="codex-session-logging",
        version="0.1.0",
        hook_events=("SessionStart",),
    )
    old_timestamp = 1_700_000_000
    for path in source.rglob("*"):
        os.utime(path, (old_timestamp, old_timestamp))

    result = update_plugin.install_plugin_if_needed(source, cache_root=tmp_path / "cache")
    installed_script = Path(result["path"]) / "scripts" / "session_start.py"

    assert result["installed"] is True
    assert installed_script.stat().st_mtime > old_timestamp


def test_install_plugin_repairs_corrupt_same_version_cache(tmp_path):
    update_plugin = load_update_plugin()
    source = write_minimal_plugin(
        tmp_path / "source",
        name="linear-progress-sync",
        version="0.3.0",
    )
    cache_root = tmp_path / "cache"

    first = update_plugin.install_plugin_if_needed(source, cache_root=cache_root)
    installed_script = Path(first["path"]) / "scripts" / "update_plugin.py"
    installed_script.unlink()

    repaired = update_plugin.install_plugin_if_needed(source, cache_root=cache_root)
    current = update_plugin.install_plugin_if_needed(source, cache_root=cache_root)

    assert repaired["installed"] is True
    assert installed_script.read_bytes() == (source / "scripts" / "update_plugin.py").read_bytes()
    assert current["installed"] is False


def test_run_update_repairs_corrupt_same_version_bootstrap_cache(tmp_path, monkeypatch):
    update_plugin = load_update_plugin()
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    cache_root = tmp_path / "cache" / "coreedge-local"
    cache_parent = cache_root / "linear-progress-sync"
    current = write_minimal_plugin(
        cache_parent,
        directory_name="0.3.0",
        version="0.3.0",
        hook_events=("SessionStart", "PreToolUse"),
    )
    damaged_script = current / "scripts" / "update_plugin.py"
    damaged_script.unlink()
    archive, digest = make_marketplace_archive(tmp_path, bootstrap_version="0.3.0")
    manifest = tmp_path / "latest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": "0.3.0",
                "archive_url": archive.as_uri(),
                "sha256": digest,
                "plugin_subdir": "plugins/linear-progress-sync",
            }
        ),
        encoding="utf-8",
    )

    result = update_plugin.run_update(
        current_plugin_root=current,
        cache_parent=cache_parent,
        manifest_url=manifest.as_uri(),
        state_path=tmp_path / "update-state.json",
        force=True,
        install_hooks=False,
    )
    second = update_plugin.run_update(
        current_plugin_root=current,
        cache_parent=cache_parent,
        manifest_url=manifest.as_uri(),
        state_path=tmp_path / "update-state.json",
        force=True,
        install_hooks=False,
    )

    assert result["updated"] is True
    assert "linear-progress-sync" in {item["name"] for item in result["installed_plugins"]}
    assert damaged_script.exists()
    assert second["updated"] is False


def test_run_update_skips_entire_older_marketplace_without_partial_installs(tmp_path, monkeypatch):
    update_plugin = load_update_plugin()
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    cache_root = tmp_path / "cache" / "coreedge-local"
    cache_parent = cache_root / "linear-progress-sync"
    current = write_minimal_plugin(
        cache_parent,
        directory_name="0.4.0",
        version="0.4.0",
    )
    write_minimal_plugin(
        cache_root / "codex-session-logging",
        name="codex-session-logging",
        directory_name="0.2.2",
        version="0.2.2",
    )
    archive, digest = make_marketplace_archive(tmp_path, bootstrap_version="0.3.0")
    manifest = tmp_path / "latest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": "0.3.0",
                "archive_url": archive.as_uri(),
                "sha256": digest,
                "plugin_subdir": "plugins/linear-progress-sync",
            }
        ),
        encoding="utf-8",
    )

    state_path = tmp_path / "update-state.json"
    update_plugin.write_update_state(state_path, {"last_error": "stale network failure"})
    result = update_plugin.run_update(
        current_plugin_root=current,
        cache_parent=cache_parent,
        manifest_url=manifest.as_uri(),
        state_path=state_path,
        force=True,
        install_hooks=False,
    )

    assert result["updated"] is False
    assert result["skipped"] == "newer_current"
    assert sorted(path.name for path in cache_parent.iterdir() if not path.name.startswith(".")) == ["0.4.0"]
    logging_parent = cache_root / "codex-session-logging"
    assert sorted(path.name for path in logging_parent.iterdir() if not path.name.startswith(".")) == ["0.2.2"]
    assert not (codex_home / "coreedge" / "marketplace" / "current").exists()
    assert "last_error" not in update_plugin.read_update_state(state_path)


def test_run_update_rolls_back_new_cache_install_when_activation_fails(tmp_path, monkeypatch):
    update_plugin = load_update_plugin()
    resident = sys.modules["resident_updater"]
    codex_home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    cache_root = tmp_path / "cache" / "coreedge-local"
    cache_parent = cache_root / "linear-progress-sync"
    current = write_minimal_plugin(
        cache_parent,
        directory_name="0.2.11",
        version="0.2.11",
    )
    archive, digest = make_marketplace_archive(tmp_path, bootstrap_version="0.3.0")
    manifest = tmp_path / "latest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": "0.3.0",
                "archive_url": archive.as_uri(),
                "sha256": digest,
                "plugin_subdir": "plugins/linear-progress-sync",
            }
        ),
        encoding="utf-8",
    )

    def fail_runtime(*_args, **_kwargs):
        raise OSError("simulated runtime failure")

    monkeypatch.setattr(resident, "install_runtime", fail_runtime)
    with pytest.raises(OSError, match="simulated runtime failure"):
        update_plugin.run_update(
            current_plugin_root=current,
            cache_parent=cache_parent,
            manifest_url=manifest.as_uri(),
            state_path=tmp_path / "update-state.json",
            force=True,
            install_hooks=False,
        )

    assert sorted(path.name for path in cache_parent.iterdir() if not path.name.startswith(".")) == ["0.2.11"]
    assert not (cache_root / "codex-session-logging").exists()


def test_update_plugin_rejects_archive_with_wrong_sha(tmp_path, monkeypatch):
    update_plugin = load_update_plugin()
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    cache_parent = tmp_path / "cache" / "coreedge-local" / "linear-progress-sync"
    current = write_minimal_plugin(cache_parent / "0.2.0", version="0.2.0")
    archive, _digest = make_plugin_archive(tmp_path, version="0.2.1")
    manifest = tmp_path / "latest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": "0.2.1",
                "archive_url": archive.as_uri(),
                "sha256": "0" * 64,
                "plugin_subdir": "plugins/linear-progress-sync",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="SHA256 mismatch"):
        update_plugin.run_update(
            current_plugin_root=current,
            cache_parent=cache_parent,
            manifest_url=manifest.as_uri(),
            state_path=tmp_path / "update-state.json",
            force=True,
            install_hooks=False,
        )

    assert not (cache_parent / "0.2.1").exists()


def test_update_plugin_checks_even_when_recently_checked(tmp_path):
    update_plugin = load_update_plugin()
    current = write_minimal_plugin(tmp_path / "current", version="0.2.0")
    archive, sha = make_plugin_archive(tmp_path, version="0.2.1")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": "0.2.1",
                "archive_url": archive.as_uri(),
                "sha256": sha,
                "plugin_subdir": "plugins/linear-progress-sync",
            }
        ),
        encoding="utf-8",
    )
    state_path = tmp_path / "update-state.json"
    update_plugin.write_update_state(
        state_path,
        {
            "last_checked_at": "2026-07-03T18:00:00+00:00",
            "last_error": "stale network failure",
        },
    )

    result = update_plugin.run_update(
        current_plugin_root=current,
        cache_parent=tmp_path / "cache",
        manifest_url=manifest.as_uri(),
        state_path=state_path,
        now=datetime(2026, 7, 3, 18, 30, tzinfo=timezone.utc),
        install_hooks=False,
    )

    assert result["updated"] is True
    assert result["installed_version"] == "0.2.1"
    assert "last_error" not in update_plugin.read_update_state(state_path)


def test_default_update_manifest_is_read_from_archive_not_stale_raw(tmp_path, monkeypatch):
    update_plugin = load_update_plugin()
    current = write_minimal_plugin(tmp_path / "current", version="0.2.4")
    archive, _sha = make_plugin_archive(tmp_path, version="0.2.5")
    state_path = tmp_path / "update-state.json"

    monkeypatch.setattr(update_plugin, "DEFAULT_ARCHIVE_URL", archive.as_uri())
    monkeypatch.setattr(
        update_plugin,
        "read_manifest",
        lambda _url: {
            "version": "0.2.4",
            "archive_url": "file:///stale-archive.zip",
            "plugin_subdir": "plugins/linear-progress-sync",
        },
    )

    result = update_plugin.run_update(
        current_plugin_root=current,
        cache_parent=tmp_path / "cache",
        manifest_url=update_plugin.DEFAULT_MANIFEST_URL,
        state_path=state_path,
        install_hooks=False,
    )

    assert result["updated"] is True
    assert result["installed_version"] == "0.2.5"
    assert result["latest_version"] == "0.2.5"


def test_maybe_spawn_auto_update_passes_state_path(tmp_path, monkeypatch):
    update_plugin = load_update_plugin()
    calls = []

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        return object()

    monkeypatch.setattr(update_plugin.subprocess, "Popen", fake_popen)
    state_path = tmp_path / "update-state.json"

    result = update_plugin.maybe_spawn_auto_update(
        plugin_root=tmp_path / "plugin",
        state_path=state_path,
    )

    assert result == {"spawned": True}
    args, kwargs = calls[0]
    assert "--plugin-root" in args
    assert "--state-path" in args
    assert str(state_path) in args
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["stdin"] == subprocess.DEVNULL


def test_resident_marketplace_activation_is_atomic_and_deterministic(tmp_path):
    resident = load_resident_updater()
    repo_root = tmp_path / "repo"
    plugins_root = repo_root / "plugins"
    write_minimal_plugin(plugins_root, name="linear-progress-sync", version="0.3.0")
    write_minimal_plugin(plugins_root, name="codex-session-logging", version="0.2.1")
    (repo_root / ".agents" / "plugins").mkdir(parents=True)
    (repo_root / ".agents" / "plugins" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "coreedge-local",
                "plugins": [
                    {
                        "name": "linear-progress-sync",
                        "source": {"source": "local", "path": "./plugins/linear-progress-sync"},
                        "policy": {"installation": "INSTALLED_BY_DEFAULT"},
                    },
                    {
                        "name": "codex-session-logging",
                        "source": {"source": "local", "path": "./plugins/codex-session-logging"},
                        "policy": {"installation": "INSTALLED_BY_DEFAULT"},
                    },
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    cache_root = tmp_path / "codex" / "plugins" / "cache" / "coreedge-local"
    write_minimal_plugin(cache_root / "linear-progress-sync", directory_name="0.2.11", version="0.2.11")
    write_minimal_plugin(cache_root / "linear-progress-sync", directory_name="0.3.0", version="0.3.0")
    write_minimal_plugin(cache_root / "codex-session-logging", name="codex-session-logging", directory_name="0.1.0", version="0.1.0")
    write_minimal_plugin(cache_root / "codex-session-logging", name="codex-session-logging", directory_name="0.2.1", version="0.2.1")
    config_path = tmp_path / "codex" / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        '[model]\nname = "gpt-test"\n\n[marketplaces.coreedge-local]\n'
        'last_updated = "old"\nsource_type = "local"\nsource = "/tmp/deleted-worktree"\n',
        encoding="utf-8",
    )

    result = resident.activate_release(
        repo_root,
        codex_home=tmp_path / "codex",
        resident_root=tmp_path / "resident",
        install_service=False,
    )
    second = resident.activate_release(
        repo_root,
        codex_home=tmp_path / "codex",
        resident_root=tmp_path / "resident",
        install_service=False,
    )

    current = tmp_path / "resident" / "marketplace" / "current"
    assert current.is_symlink()
    assert current.resolve().name == "0.3.0"
    assert (current / ".agents" / "plugins" / "marketplace.json").exists()
    assert sorted(path.name for path in (cache_root / "linear-progress-sync").iterdir() if not path.name.startswith(".")) == ["0.3.0"]
    assert sorted(path.name for path in (cache_root / "codex-session-logging").iterdir() if not path.name.startswith(".")) == ["0.2.1"]
    assert (tmp_path / "resident" / "rollback" / "cache" / "linear-progress-sync" / "0.2.11").exists()
    assert (tmp_path / "resident" / "rollback" / "cache" / "codex-session-logging" / "0.1.0").exists()
    config = config_path.read_text(encoding="utf-8")
    assert '[model]\nname = "gpt-test"' in config
    assert f'source = "{current}"' in config
    assert "/tmp/deleted-worktree" not in config
    assert result["changed"] is True
    assert second["changed"] is False
    assert json.loads(json.dumps(result))["version"] == "0.3.0"


def test_resident_activation_rolls_back_cache_config_and_pointer_on_failure(tmp_path, monkeypatch):
    resident = load_resident_updater()
    old_repo = tmp_path / "old-repo"
    new_repo = tmp_path / "new-repo"
    for repo, version in ((old_repo, "0.2.11"), (new_repo, "0.3.0")):
        write_minimal_plugin(repo / "plugins", name="linear-progress-sync", version=version)
        (repo / ".agents" / "plugins").mkdir(parents=True)
        (repo / ".agents" / "plugins" / "marketplace.json").write_text(
            json.dumps(
                {
                    "name": "coreedge-local",
                    "plugins": [
                        {
                            "name": "linear-progress-sync",
                            "source": {"source": "local", "path": "./plugins/linear-progress-sync"},
                            "policy": {"installation": "INSTALLED_BY_DEFAULT"},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
    codex_home = tmp_path / "codex"
    cache_root = codex_home / "plugins" / "cache" / "coreedge-local"
    write_minimal_plugin(cache_root / "linear-progress-sync", directory_name="0.2.11", version="0.2.11")
    config_path = codex_home / "config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        '[marketplaces.coreedge-local]\nsource_type = "local"\nsource = "/old/source"\n',
        encoding="utf-8",
    )
    os.chmod(config_path, 0o600)
    resident_root = tmp_path / "resident"
    resident.activate_release(
        old_repo,
        codex_home=codex_home,
        resident_root=resident_root,
        install_service=False,
    )
    write_minimal_plugin(cache_root / "linear-progress-sync", directory_name="0.3.0", version="0.3.0")
    before_config = config_path.read_text(encoding="utf-8")
    before_target = (resident_root / "marketplace" / "current").resolve()
    before_runtime = (resident_root / "runtime" / "current").resolve()

    def fail_after_cache(*_args, **_kwargs):
        raise OSError("simulated activation failure")

    monkeypatch.setattr(resident, "install_runtime", fail_after_cache)
    with pytest.raises(OSError, match="simulated activation failure"):
        resident.activate_release(
            new_repo,
            codex_home=codex_home,
            resident_root=resident_root,
            install_service=False,
        )

    assert config_path.read_text(encoding="utf-8") == before_config
    assert config_path.stat().st_mode & 0o777 == 0o600
    assert (resident_root / "marketplace" / "current").resolve() == before_target
    assert (resident_root / "runtime" / "current").resolve() == before_runtime
    assert (cache_root / "linear-progress-sync" / "0.2.11").exists()
    assert (cache_root / "linear-progress-sync" / "0.3.0").exists()


def test_cache_activation_validates_every_plugin_before_moving_any_cache(tmp_path):
    resident = load_resident_updater()
    source_root = tmp_path / "source"
    linear_source = write_minimal_plugin(
        source_root,
        name="linear-progress-sync",
        version="0.3.0",
    )
    logging_source = write_minimal_plugin(
        source_root,
        name="codex-session-logging",
        version="0.2.1",
    )
    cache_root = tmp_path / "cache"
    write_minimal_plugin(
        cache_root / "linear-progress-sync",
        directory_name="0.2.11",
        version="0.2.11",
    )
    shutil.copytree(linear_source, cache_root / "linear-progress-sync" / "0.3.0")

    with pytest.raises(FileNotFoundError):
        resident.activate_plugin_caches(
            [
                {"name": "linear-progress-sync", "version": "0.3.0", "source": linear_source},
                {"name": "codex-session-logging", "version": "0.2.1", "source": logging_source},
            ],
            cache_root=cache_root,
            rollback_root=tmp_path / "rollback",
        )

    assert (cache_root / "linear-progress-sync" / "0.2.11").exists()
    assert not (tmp_path / "rollback" / "linear-progress-sync" / "0.2.11").exists()


def test_cache_activation_restores_partial_moves_after_filesystem_failure(tmp_path, monkeypatch):
    resident = load_resident_updater()
    source_root = tmp_path / "source"
    cache_root = tmp_path / "cache"
    plugins = []
    for name, old_version, version in (
        ("linear-progress-sync", "0.2.11", "0.3.0"),
        ("codex-session-logging", "0.1.0", "0.2.1"),
    ):
        source = write_minimal_plugin(source_root, name=name, version=version)
        write_minimal_plugin(cache_root / name, name=name, directory_name=old_version, version=old_version)
        shutil.copytree(source, cache_root / name / version)
        plugins.append({"name": name, "version": version, "source": source})

    original_move = resident.shutil.move
    move_calls = 0

    def fail_second_move(source, destination):
        nonlocal move_calls
        move_calls += 1
        if move_calls == 2:
            raise OSError("simulated cache move failure")
        return original_move(source, destination)

    monkeypatch.setattr(resident.shutil, "move", fail_second_move)
    with pytest.raises(OSError, match="simulated cache move failure"):
        resident.activate_plugin_caches(
            plugins,
            cache_root=cache_root,
            rollback_root=tmp_path / "rollback",
        )

    assert (cache_root / "linear-progress-sync" / "0.2.11").exists()
    assert (cache_root / "codex-session-logging" / "0.1.0").exists()
    assert not (tmp_path / "rollback" / "linear-progress-sync" / "0.2.11").exists()


def test_resident_activation_repairs_corrupt_managed_release_and_runtime(tmp_path):
    resident = load_resident_updater()
    repo = tmp_path / "repo"
    write_minimal_plugin(repo / "plugins", name="linear-progress-sync", version="0.3.0")
    (repo / ".agents" / "plugins").mkdir(parents=True)
    (repo / ".agents" / "plugins" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "coreedge-local",
                "plugins": [
                    {
                        "name": "linear-progress-sync",
                        "source": {"source": "local", "path": "./plugins/linear-progress-sync"},
                        "policy": {"installation": "INSTALLED_BY_DEFAULT"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    codex_home = tmp_path / "codex"
    write_minimal_plugin(
        codex_home / "plugins" / "cache" / "coreedge-local" / "linear-progress-sync",
        directory_name="0.3.0",
        version="0.3.0",
    )
    resident_root = tmp_path / "resident"
    resident.activate_release(
        repo,
        codex_home=codex_home,
        resident_root=resident_root,
        install_service=False,
    )
    managed_manifest = (
        resident_root
        / "marketplace"
        / "releases"
        / "0.3.0"
        / ".agents"
        / "plugins"
        / "marketplace.json"
    )
    runtime_script = resident_root / "runtime" / "releases" / "0.3.0" / "update_plugin.py"
    managed_hook = (
        resident_root
        / "marketplace"
        / "releases"
        / "0.3.0"
        / "plugins"
        / "linear-progress-sync"
        / "hooks"
        / "hooks.json"
    )
    runtime_script.write_text("VERSION = 'valid but corrupt'\n", encoding="utf-8")
    managed_hook.write_text("{}\n", encoding="utf-8")

    result = resident.activate_release(
        repo,
        codex_home=codex_home,
        resident_root=resident_root,
        install_service=False,
    )

    assert result["changed"] is True
    assert json.loads(managed_manifest.read_text(encoding="utf-8"))["name"] == "coreedge-local"
    assert runtime_script.read_bytes() == (repo / "plugins" / "linear-progress-sync" / "scripts" / "update_plugin.py").read_bytes()
    assert managed_hook.read_bytes() == (repo / "plugins" / "linear-progress-sync" / "hooks" / "hooks.json").read_bytes()


def test_resident_activation_restores_replaced_same_version_release_on_failure(tmp_path, monkeypatch):
    resident = load_resident_updater()
    original_repo = tmp_path / "original"
    changed_repo = tmp_path / "changed"
    for repo, marker in ((original_repo, "original"), (changed_repo, "changed")):
        plugin = write_minimal_plugin(repo / "plugins", name="linear-progress-sync", version="0.3.0")
        (plugin / "hooks" / "hooks.json").write_text(
            json.dumps({"hooks": {}, "marker": marker}) + "\n",
            encoding="utf-8",
        )
        (repo / ".agents" / "plugins").mkdir(parents=True)
        (repo / ".agents" / "plugins" / "marketplace.json").write_text(
            json.dumps(
                {
                    "name": "coreedge-local",
                    "plugins": [
                        {
                            "name": "linear-progress-sync",
                            "source": {"source": "local", "path": "./plugins/linear-progress-sync"},
                            "policy": {"installation": "INSTALLED_BY_DEFAULT"},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
    codex_home = tmp_path / "codex"
    resident_root = tmp_path / "resident"
    resident.activate_release(
        original_repo,
        codex_home=codex_home,
        resident_root=resident_root,
        install_service=False,
    )

    real_copy_release = resident.copy_marketplace_release

    def copy_then_fail_validation(*args, **kwargs):
        result = real_copy_release(*args, **kwargs)

        def fail_validation(*_args, **_kwargs):
            raise OSError("simulated post-staging validation failure")

        monkeypatch.setattr(resident, "marketplace_plugins", fail_validation)
        return result

    monkeypatch.setattr(resident, "copy_marketplace_release", copy_then_fail_validation)
    with pytest.raises(OSError, match="post-staging validation failure"):
        resident.activate_release(
            changed_repo,
            codex_home=codex_home,
            resident_root=resident_root,
            install_service=False,
        )

    managed_hook = resident_root / "marketplace" / "current" / "plugins" / "linear-progress-sync" / "hooks" / "hooks.json"
    cache_hook = codex_home / "plugins" / "cache" / "coreedge-local" / "linear-progress-sync" / "0.3.0" / "hooks" / "hooks.json"
    assert json.loads(managed_hook.read_text(encoding="utf-8"))["marker"] == "original"
    assert json.loads(cache_hook.read_text(encoding="utf-8"))["marker"] == "original"


def test_resident_activation_restores_same_version_runtime_when_service_setup_fails(tmp_path, monkeypatch):
    resident = load_resident_updater()
    original_repo = tmp_path / "original"
    plugin = write_minimal_plugin(
        original_repo / "plugins",
        name="linear-progress-sync",
        version="0.3.0",
    )
    (original_repo / ".agents" / "plugins").mkdir(parents=True)
    (original_repo / ".agents" / "plugins" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "coreedge-local",
                "plugins": [
                    {
                        "name": "linear-progress-sync",
                        "source": {"source": "local", "path": "./plugins/linear-progress-sync"},
                        "policy": {"installation": "INSTALLED_BY_DEFAULT"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    changed_repo = tmp_path / "changed"
    shutil.copytree(original_repo, changed_repo)
    changed_script = changed_repo / "plugins" / "linear-progress-sync" / "scripts" / "update_plugin.py"
    changed_script.write_text("VERSION = 'changed-same-version'\n", encoding="utf-8")
    codex_home = tmp_path / "codex"
    resident_root = tmp_path / "resident"
    resident.activate_release(
        original_repo,
        codex_home=codex_home,
        resident_root=resident_root,
        install_service=False,
    )
    runtime_script = resident_root / "runtime" / "current" / "update_plugin.py"
    original_runtime = runtime_script.read_bytes()

    def fail_service(*_args, **_kwargs):
        raise OSError("simulated service setup failure")

    def fail_config_restore(*_args, **_kwargs):
        raise PermissionError("simulated config rollback failure")

    monkeypatch.setattr(resident, "ensure_resident_updater", fail_service)
    monkeypatch.setattr(resident, "restore_file", fail_config_restore)
    with pytest.raises(RuntimeError, match="marketplace config: simulated config rollback failure") as exc_info:
        resident.activate_release(
            changed_repo,
            codex_home=codex_home,
            resident_root=resident_root,
            install_service=True,
        )
    assert isinstance(exc_info.value.__cause__, OSError)
    assert "service setup failure" in str(exc_info.value.__cause__)

    assert runtime_script.read_bytes() == original_runtime
    managed_script = resident_root / "marketplace" / "current" / "plugins" / "linear-progress-sync" / "scripts" / "update_plugin.py"
    cache_script = codex_home / "plugins" / "cache" / "coreedge-local" / "linear-progress-sync" / "0.3.0" / "scripts" / "update_plugin.py"
    assert managed_script.read_bytes() == original_runtime
    assert cache_script.read_bytes() == original_runtime


def test_resident_marketplace_rejects_source_path_escape(tmp_path):
    resident = load_resident_updater()
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    write_minimal_plugin(outside, name="linear-progress-sync", version="0.3.0")
    (repo / ".agents" / "plugins").mkdir(parents=True)
    (repo / ".agents" / "plugins" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "coreedge-local",
                "plugins": [
                    {
                        "name": "linear-progress-sync",
                        "source": {"source": "local", "path": "../outside/linear-progress-sync"},
                        "policy": {"installation": "INSTALLED_BY_DEFAULT"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="escapes marketplace root"):
        resident.marketplace_plugins(repo)


def test_real_marketplace_activates_in_isolated_codex_home_and_passes_doctor(tmp_path):
    resident = load_resident_updater()
    codex_home = tmp_path / "isolated codex home"
    cache_root = codex_home / "plugins" / "cache" / "coreedge-local"
    for plugin in resident.marketplace_plugins(ROOT):
        destination = cache_root / str(plugin["name"]) / str(plugin["version"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(Path(plugin["source"]), destination)
    resident_root = tmp_path / "isolated resident home"

    activation = resident.activate_release(
        ROOT,
        codex_home=codex_home,
        resident_root=resident_root,
        install_service=True,
        platform="linux",
    )
    health = resident.doctor(
        codex_home=codex_home,
        resident_root=resident_root,
        platform="linux",
    )

    assert activation["version"] == "0.3.2"
    assert health["healthy"] is True
    assert health["issues"] == []
    assert health["cache_versions"] == {
        "codex-session-logging": ["0.2.3"],
        "linear-progress-sync": ["0.3.2"],
    }
    assert subprocess.run(["sh", "-n", str(resident_root / "run.sh")], check=False).returncode == 0


def test_resident_hook_repairs_matching_cache_and_runtime_corruption_from_managed_release(tmp_path):
    resident = load_resident_updater()
    codex_home = tmp_path / "codex"
    resident_root = tmp_path / "resident"
    resident.activate_release(
        ROOT,
        codex_home=codex_home,
        resident_root=resident_root,
        install_service=False,
        platform="linux",
    )
    managed = resident_root / "marketplace/current/plugins/linear-progress-sync"
    cache = codex_home / "plugins/cache/coreedge-local/linear-progress-sync/0.3.2"
    runtime = resident_root / "runtime/current"
    corrupt_content = (managed / "scripts/linear_sync.py").read_bytes()
    (cache / "scripts/update_plugin.py").write_bytes(corrupt_content)
    (runtime / "update_plugin.py").write_bytes(corrupt_content)

    result = resident.ensure_resident_updater(
        cache,
        codex_home=codex_home,
        resident_root=resident_root,
        platform="linux",
    )
    health = resident.doctor(
        codex_home=codex_home,
        resident_root=resident_root,
        platform="linux",
    )

    assert result["changed"] is True
    assert [item["name"] for item in result["repaired_caches"]] == ["linear-progress-sync"]
    assert (runtime / "update_plugin.py").read_bytes() == (managed / "scripts/update_plugin.py").read_bytes()
    assert (cache / "scripts/update_plugin.py").read_bytes() == (managed / "scripts/update_plugin.py").read_bytes()
    assert health["healthy"] is True


def test_resident_runtime_and_launch_agent_are_idempotent(tmp_path, monkeypatch):
    resident = load_resident_updater()
    plugin_root = write_minimal_plugin(
        tmp_path / "plugins",
        name="linear-progress-sync",
        version="0.3.0",
    )
    calls = []

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return Completed()

    monkeypatch.setattr(resident.os, "getuid", lambda: 501)
    first = resident.ensure_resident_updater(
        plugin_root,
        codex_home=tmp_path / "codex",
        resident_root=tmp_path / "resident",
        launch_agents_dir=tmp_path / "LaunchAgents",
        platform="darwin",
        runner=runner,
    )
    second = resident.ensure_resident_updater(
        plugin_root,
        codex_home=tmp_path / "codex",
        resident_root=tmp_path / "resident",
        launch_agents_dir=tmp_path / "LaunchAgents",
        platform="darwin",
        runner=runner,
    )

    runtime_current = tmp_path / "resident" / "runtime" / "current"
    plist = tmp_path / "LaunchAgents" / "com.coreedge.codex-plugins-updater.plist"
    presence_plist = tmp_path / "LaunchAgents" / "com.coreedge.codex-session-presence.plist"
    assert runtime_current.is_symlink()
    assert (runtime_current / "update_plugin.py").exists()
    assert (runtime_current / "linear_sync.py").exists()
    assert (runtime_current / "resident_updater.py").exists()
    assert (tmp_path / "resident" / "run.sh").stat().st_mode & 0o111
    assert subprocess.run(
        ["sh", "-n", str(tmp_path / "resident" / "run.sh")],
        check=False,
    ).returncode == 0
    plist_text = plist.read_text(encoding="utf-8")
    assert "RunAtLoad" in plist_text
    assert "StartInterval" in plist_text
    assert "1800" in plist_text
    assert "com.coreedge.codex-plugins-updater" in plist_text
    presence_text = presence_plist.read_text(encoding="utf-8")
    assert "RunAtLoad" in presence_text
    assert "StartInterval" in presence_text
    assert "60" in presence_text
    assert "com.coreedge.codex-session-presence" in presence_text
    assert (tmp_path / "resident" / "presence.sh").stat().st_mode & 0o111
    assert subprocess.run(
        ["sh", "-n", str(tmp_path / "resident" / "presence.sh")],
        check=False,
    ).returncode == 0
    assert first["changed"] is True
    assert second["changed"] is False
    assert first["presence"]["scheduled"] is True
    assert second["presence"]["scheduled"] is True
    assert len(calls) == 4
    assert calls[0][0][:3] == ["launchctl", "bootstrap", "gui/501"]
    assert calls[1][0][:3] == ["launchctl", "bootstrap", "gui/501"]
    assert calls[2][0][:3] == ["launchctl", "print", "gui/501/com.coreedge.codex-session-presence"]
    assert calls[3][0][:3] == ["launchctl", "print", "gui/501/com.coreedge.codex-plugins-updater"]


def test_resident_runner_bootstraps_from_cached_plugin_before_managed_marketplace_exists(tmp_path):
    resident = load_resident_updater()
    plugin_root = write_minimal_plugin(
        tmp_path / "cached $plugins",
        name="linear-progress-sync",
        version="0.3.0",
    )
    capture_path = tmp_path / "runner-args.json"
    (plugin_root / "scripts" / "update_plugin.py").write_text(
        "import json, os, sys\n"
        "open(os.environ['RUNNER_CAPTURE'], 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    resident_root = tmp_path / "resident $home"
    resident.ensure_resident_updater(
        plugin_root,
        codex_home=tmp_path / "codex $home",
        resident_root=resident_root,
        platform="linux",
    )

    completed = subprocess.run(
        [str(resident_root / "run.sh")],
        env={**os.environ, "RUNNER_CAPTURE": str(capture_path)},
        check=False,
    )
    args = json.loads(capture_path.read_text(encoding="utf-8"))

    assert completed.returncode == 0
    assert args[args.index("--plugin-root") + 1] == str(plugin_root.resolve())
    assert "--resident" in args
    assert not (resident_root / "marketplace" / "current").exists()


def test_resident_runner_discovers_surviving_cache_when_fixed_bootstrap_disappears(tmp_path):
    resident = load_resident_updater()
    bootstrap_root = write_minimal_plugin(
        tmp_path / "bootstrap plugins",
        name="linear-progress-sync",
        version="0.3.0",
    )
    capture_path = tmp_path / "runner-fallback-args.json"
    (bootstrap_root / "scripts" / "update_plugin.py").write_text(
        "import json, os, sys\n"
        "open(os.environ['RUNNER_CAPTURE'], 'w', encoding='utf-8').write(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    codex_home = tmp_path / "codex home"
    resident_root = tmp_path / "resident home"
    resident.ensure_resident_updater(
        bootstrap_root,
        codex_home=codex_home,
        resident_root=resident_root,
        platform="linux",
    )
    surviving_cache = write_minimal_plugin(
        codex_home / "plugins" / "cache" / "coreedge-local" / "linear-progress-sync",
        directory_name="0.2.1",
        name="linear-progress-sync",
        version="0.2.1",
    )
    shutil.rmtree(bootstrap_root)

    completed = subprocess.run(
        [str(resident_root / "run.sh")],
        env={**os.environ, "RUNNER_CAPTURE": str(capture_path)},
        check=False,
    )
    args = json.loads(capture_path.read_text(encoding="utf-8"))

    assert completed.returncode == 0
    assert args[args.index("--plugin-root") + 1] == str(surviving_cache.resolve())
    assert "--resident" in args
    assert not (resident_root / "marketplace" / "current").exists()


def test_presence_runner_follows_managed_marketplace_without_hook_reload(tmp_path):
    resident = load_resident_updater()
    plugin_root = write_minimal_plugin(
        tmp_path / "bootstrap plugins",
        name="linear-progress-sync",
        version="0.3.0",
    )
    codex_home = tmp_path / "codex home"
    resident_root = tmp_path / "resident home"
    resident.ensure_resident_updater(
        plugin_root,
        codex_home=codex_home,
        resident_root=resident_root,
        platform="linux",
    )
    capture = tmp_path / "presence-ran"
    managed_script = (
        resident_root
        / "marketplace"
        / "current"
        / "plugins"
        / "codex-session-logging"
        / "scripts"
        / "publish_presence.py"
    )
    managed_script.parent.mkdir(parents=True)
    managed_script.write_text(
        "import os\nfrom pathlib import Path\nPath(os.environ['PRESENCE_CAPTURE']).write_text('managed')\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [str(resident_root / "presence.sh")],
        env={**os.environ, "PRESENCE_CAPTURE": str(capture)},
        check=False,
    )

    assert completed.returncode == 0
    assert capture.read_text(encoding="utf-8") == "managed"


def test_presence_runner_discovers_cached_logger_before_managed_marketplace_exists(tmp_path):
    resident = load_resident_updater()
    plugin_root = write_minimal_plugin(
        tmp_path / "bootstrap plugins",
        name="linear-progress-sync",
        version="0.3.0",
    )
    codex_home = tmp_path / "codex home"
    resident_root = tmp_path / "resident home"
    resident.ensure_resident_updater(
        plugin_root,
        codex_home=codex_home,
        resident_root=resident_root,
        platform="linux",
    )
    capture = tmp_path / "presence-ran"
    cached_script = (
        codex_home
        / "plugins"
        / "cache"
        / "coreedge-local"
        / "codex-session-logging"
        / "0.2.3"
        / "scripts"
        / "publish_presence.py"
    )
    cached_script.parent.mkdir(parents=True)
    cached_manifest = cached_script.parents[1] / ".codex-plugin" / "plugin.json"
    cached_manifest.parent.mkdir(parents=True)
    cached_manifest.write_text(
        json.dumps({"name": "codex-session-logging", "version": "0.2.3"}),
        encoding="utf-8",
    )
    cached_script.write_text(
        "import os\nfrom pathlib import Path\nPath(os.environ['PRESENCE_CAPTURE']).write_text('cache')\n",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [str(resident_root / "presence.sh")],
        env={**os.environ, "PRESENCE_CAPTURE": str(capture)},
        check=False,
    )

    assert completed.returncode == 0
    assert capture.read_text(encoding="utf-8") == "cache"


def test_presence_runner_uses_semantic_coreedge_cache_and_rejects_other_marketplaces(tmp_path):
    resident = load_resident_updater()
    plugin_root = write_minimal_plugin(tmp_path / "bootstrap", version="0.3.0")
    codex_home = tmp_path / "codex"
    resident_root = tmp_path / "resident"
    resident.ensure_resident_updater(
        plugin_root,
        codex_home=codex_home,
        resident_root=resident_root,
        platform="linux",
    )
    for marketplace, version, marker in (
        ("coreedge-local", "0.2.9", "old"),
        ("coreedge-local", "0.2.10", "new"),
        ("zzz-untrusted", "99.0.0", "untrusted"),
    ):
        root = codex_home / "plugins" / "cache" / marketplace / "codex-session-logging" / version
        manifest = root / ".codex-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            json.dumps({"name": "codex-session-logging", "version": version}),
            encoding="utf-8",
        )
        script = root / "scripts" / "publish_presence.py"
        script.parent.mkdir(parents=True)
        script.write_text(
            f"import os\nfrom pathlib import Path\nPath(os.environ['PRESENCE_CAPTURE']).write_text({marker!r})\n",
            encoding="utf-8",
        )
    capture = tmp_path / "capture"

    completed = subprocess.run(
        [str(resident_root / "presence.sh")],
        env={**os.environ, "PRESENCE_CAPTURE": str(capture)},
        check=False,
    )

    assert completed.returncode == 0
    assert capture.read_text(encoding="utf-8") == "new"


def test_presence_runner_honors_persisted_opt_out_without_shell_environment(tmp_path):
    resident = load_resident_updater()
    plugin_root = write_minimal_plugin(tmp_path / "bootstrap", version="0.3.0")
    codex_home = tmp_path / "codex"
    resident_root = tmp_path / "resident"
    resident.ensure_resident_updater(
        plugin_root,
        codex_home=codex_home,
        resident_root=resident_root,
        platform="linux",
    )
    cached_plugin = codex_home / "plugins" / "cache" / "coreedge-local" / "codex-session-logging" / "0.2.3"
    cached_plugin.parent.mkdir(parents=True)
    shutil.copytree(ROOT / "plugins" / "codex-session-logging", cached_plugin)
    preference = codex_home / "session-logging" / "preferences.json"
    preference.parent.mkdir(parents=True, exist_ok=True)
    preference.write_text(json.dumps({"enabled": False}), encoding="utf-8")
    environment = {key: value for key, value in os.environ.items() if key != "CODEX_SESSION_LOG_AUTO_UPLOAD"}

    completed = subprocess.run(
        [str(resident_root / "presence.sh")],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    state = json.loads(
        (codex_home / "coreedge" / "presence" / "state.json").read_text(encoding="utf-8")
    )

    assert completed.returncode == 0, completed.stderr
    assert state["last_result"] == "disabled"
    assert state.get("database") is None


def test_resident_persists_only_explicit_upload_opt_out(tmp_path, monkeypatch):
    resident = load_resident_updater()
    codex_home = tmp_path / "codex"
    resident_root = tmp_path / "resident"
    pending = codex_home / "session-logging" / "queue" / "record.json"
    pending.parent.mkdir(parents=True)
    pending.write_text("{}", encoding="utf-8")
    preference_path = codex_home / "session-logging" / "preferences.json"
    preference_path.parent.mkdir(parents=True, exist_ok=True)
    preference_path.write_text(json.dumps({"enabled": True}), encoding="utf-8")
    monkeypatch.setenv("CODEX_SESSION_LOG_AUTO_UPLOAD", "0")

    changed = resident.migrate_session_upload_preference(
        codex_home=codex_home,
        resident_root=resident_root,
    )
    preference = json.loads(preference_path.read_text(encoding="utf-8"))

    assert changed is True
    assert preference == {"enabled": False, "migrated_from": "environment"}
    monkeypatch.setenv("CODEX_SESSION_LOG_AUTO_UPLOAD", "1")
    reenabled = resident.migrate_session_upload_preference(
        codex_home=codex_home,
        resident_root=resident_root,
    )
    unchanged = resident.migrate_session_upload_preference(
        codex_home=codex_home,
        resident_root=resident_root,
    )
    assert reenabled is True
    assert unchanged is False
    assert json.loads(preference_path.read_text(encoding="utf-8")) == {
        "enabled": True,
        "migrated_from": "environment",
    }


def test_resident_does_not_treat_transient_queue_as_upload_opt_out(tmp_path, monkeypatch):
    resident = load_resident_updater()
    codex_home = tmp_path / "codex"
    resident_root = tmp_path / "resident"
    pending = codex_home / "session-logging" / "queue" / "pending" / "record.json"
    pending.parent.mkdir(parents=True)
    pending.write_text("{}", encoding="utf-8")
    monkeypatch.delenv("CODEX_SESSION_LOG_AUTO_UPLOAD", raising=False)

    changed = resident.migrate_session_upload_preference(
        codex_home=codex_home,
        resident_root=resident_root,
    )

    assert changed is False
    assert not (codex_home / "session-logging" / "preferences.json").exists()


def test_environment_opt_out_is_persisted_for_resident_launches(tmp_path, monkeypatch):
    resident = load_resident_updater()
    update_plugin = load_update_plugin()
    plugin_root = write_minimal_plugin(
        tmp_path / "plugins",
        name="linear-progress-sync",
        version="0.3.0",
    )
    config_dir = tmp_path / "linear config"
    monkeypatch.setenv("LINEAR_SYNC_AUTO_UPDATE", "0")
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(config_dir))

    resident.ensure_resident_updater(
        plugin_root,
        codex_home=tmp_path / "codex",
        resident_root=tmp_path / "resident",
        platform="linux",
    )
    state = json.loads((config_dir / "update.json").read_text(encoding="utf-8"))
    monkeypatch.delenv("LINEAR_SYNC_AUTO_UPDATE")

    assert state["enabled"] is False
    assert update_plugin.auto_update_enabled(state) is False


def test_auto_update_preference_cli_round_trips_without_network(tmp_path):
    state_path = tmp_path / "update.json"
    commands = (
        ("--disable-auto-update", False),
        ("--enable-auto-update", True),
    )

    for flag, expected in commands:
        completed = subprocess.run(
            [sys.executable, str(UPDATE_PATH), "--state-path", str(state_path), flag, "--json"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        payload = json.loads(completed.stdout)

        assert completed.returncode == 0, completed.stderr
        assert payload["enabled"] is expected
        assert json.loads(state_path.read_text(encoding="utf-8"))["enabled"] is expected


def test_resident_launch_agent_recovers_when_plist_exists_but_job_is_unloaded(tmp_path, monkeypatch):
    resident = load_resident_updater()
    plugin_root = write_minimal_plugin(
        tmp_path / "plugins",
        name="linear-progress-sync",
        version="0.3.0",
    )
    launch_agents = tmp_path / "LaunchAgents"
    calls = []

    class Completed:
        stdout = ""
        stderr = ""

        def __init__(self, returncode):
            self.returncode = returncode

    def runner(args, **_kwargs):
        calls.append(args)
        return Completed(1 if args[1] in {"print", "kickstart"} else 0)

    monkeypatch.setattr(resident.os, "getuid", lambda: 501)
    resident.ensure_resident_updater(
        plugin_root,
        codex_home=tmp_path / "codex",
        resident_root=tmp_path / "resident",
        launch_agents_dir=launch_agents,
        platform="darwin",
        runner=runner,
    )
    calls.clear()
    result = resident.ensure_resident_updater(
        plugin_root,
        codex_home=tmp_path / "codex",
        resident_root=tmp_path / "resident",
        launch_agents_dir=launch_agents,
        platform="darwin",
        runner=runner,
    )

    assert result["scheduled"] is True
    updater_calls = [call for call in calls if "codex-plugins-updater" in " ".join(call)]
    assert updater_calls[0][:3] == ["launchctl", "print", "gui/501/com.coreedge.codex-plugins-updater"]
    assert updater_calls[1][1] == "kickstart"
    assert updater_calls[2][:3] == ["launchctl", "bootstrap", "gui/501"]


def test_changed_launch_agent_is_not_marked_active_when_old_job_cannot_unload(tmp_path, monkeypatch):
    resident = load_resident_updater()
    calls = []

    class Completed:
        stdout = ""
        stderr = "still loaded"

        def __init__(self, returncode):
            self.returncode = returncode

    def runner(args, **_kwargs):
        calls.append(args)
        if args[1] == "bootout":
            return Completed(1)
        if args[1] == "print":
            return Completed(0)
        return Completed(0)

    monkeypatch.setattr(resident.os, "getuid", lambda: 501)
    scheduled, error = resident.schedule_launch_agent(
        label=resident.PRESENCE_SERVICE_LABEL,
        plist_path=tmp_path / "presence.plist",
        plist_existed=True,
        plist_changed=True,
        runner=runner,
    )

    assert scheduled is False
    assert error == "still loaded"
    assert [call[1] for call in calls] == ["bootout", "print"]


def test_marketplace_config_migration_supports_quoted_section_and_preserves_comments(tmp_path):
    resident = load_resident_updater()
    config = tmp_path / "config.toml"
    config.write_text(
        '# personal setting\n[marketplaces."coreedge-local"]\n'
        'source_type = "git" # migrated\nsource = "/old" # stale\n\n[other]\nenabled = true\n',
        encoding="utf-8",
    )

    changed = resident.update_marketplace_config(config, tmp_path / "managed" / "current")
    second = resident.update_marketplace_config(config, tmp_path / "managed" / "current")
    text = config.read_text(encoding="utf-8")

    assert changed is True
    assert second is False
    assert "# personal setting" in text
    assert '[marketplaces."coreedge-local"]' in text
    assert 'source_type = "local"' in text
    expected_source = tmp_path / "managed" / "current"
    assert f'source = "{expected_source}"' in text
    assert "[other]\nenabled = true" in text


def test_resident_installer_degrades_safely_off_macos(tmp_path):
    resident = load_resident_updater()
    plugin_root = write_minimal_plugin(
        tmp_path / "plugins",
        name="linear-progress-sync",
        version="0.3.0",
    )

    result = resident.ensure_resident_updater(
        plugin_root,
        codex_home=tmp_path / "codex",
        resident_root=tmp_path / "resident",
        platform="linux",
    )

    assert result["installed"] is True
    assert result["scheduled"] is False
    assert result["reason"] == "unsupported_platform"


def test_resident_doctor_reports_activation_drift(tmp_path):
    resident = load_resident_updater()
    codex_home = tmp_path / "codex"
    resident_root = tmp_path / "resident"
    current = resident_root / "marketplace" / "current"
    current.parent.mkdir(parents=True)
    broken_target = resident_root / "marketplace" / "releases" / "missing"
    os.symlink(broken_target, current)
    codex_home.mkdir(parents=True)
    (codex_home / "config.toml").write_text(
        '[marketplaces.coreedge-local]\nsource_type = "local"\nsource = "/tmp/stale"\n',
        encoding="utf-8",
    )

    result = resident.doctor(
        codex_home=codex_home,
        resident_root=resident_root,
        launch_agents_dir=tmp_path / "LaunchAgents",
        platform="darwin",
    )

    assert result["healthy"] is False
    assert "managed marketplace pointer is missing or broken" in result["issues"]
    assert "marketplace config does not point at the managed current release" in result["issues"]
    assert "resident updater LaunchAgent is not installed" in result["issues"]
    assert "resident session presence runner is missing or not executable" in result["issues"]
    assert "resident session presence LaunchAgent is not installed" in result["issues"]


def test_resident_doctor_reports_content_corruption_and_unloaded_service(tmp_path, monkeypatch):
    resident = load_resident_updater()
    codex_home = tmp_path / "codex"
    resident_root = tmp_path / "resident"
    cache_root = codex_home / "plugins" / "cache" / "coreedge-local"
    for plugin in resident.marketplace_plugins(ROOT):
        destination = cache_root / str(plugin["name"]) / str(plugin["version"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(Path(plugin["source"]), destination)
    launch_agents = tmp_path / "LaunchAgents"

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(resident.os, "getuid", lambda: 501)
    resident.activate_release(
        ROOT,
        codex_home=codex_home,
        resident_root=resident_root,
        launch_agents_dir=launch_agents,
        platform="darwin",
        runner=lambda *_args, **_kwargs: Completed(),
    )
    broken_cache_script = (
        cache_root
        / "linear-progress-sync"
        / "0.3.2"
        / "scripts"
        / "update_plugin.py"
    )
    broken_cache_script.write_text("VERSION = 'corrupt but valid'\n", encoding="utf-8")

    class Unloaded:
        returncode = 1
        stdout = ""
        stderr = "not loaded"

    result = resident.doctor(
        codex_home=codex_home,
        resident_root=resident_root,
        launch_agents_dir=launch_agents,
        platform="darwin",
        runner=lambda *_args, **_kwargs: Unloaded(),
    )

    assert result["healthy"] is False
    assert "linear-progress-sync cache content is incomplete or corrupt" in result["issues"]
    assert "resident updater LaunchAgent is installed but not loaded" in result["issues"]
    assert "resident session presence LaunchAgent is installed but not loaded" in result["issues"]


def test_resident_doctor_reports_stale_failed_presence_and_queue(tmp_path, monkeypatch):
    resident = load_resident_updater()
    codex_home = tmp_path / "codex"
    resident_root = tmp_path / "resident"
    launch_agents = tmp_path / "LaunchAgents"

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(resident.os, "getuid", lambda: 501)
    resident.activate_release(
        ROOT,
        codex_home=codex_home,
        resident_root=resident_root,
        launch_agents_dir=launch_agents,
        platform="darwin",
        runner=lambda *_args, **_kwargs: Completed(),
    )
    state_path = codex_home / "coreedge" / "presence" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "last_checked_at": "2020-01-01T00:00:00+00:00",
                "last_result": "retrying",
                "last_error": "network unavailable",
            }
        ),
        encoding="utf-8",
    )
    pending = codex_home / "session-logging" / "presence-queue" / "pending" / "event.json"
    pending.parent.mkdir(parents=True)
    pending.write_text("{}", encoding="utf-8")
    dead = codex_home / "session-logging" / "presence-queue" / "dead-letter" / "bad.json"
    dead.parent.mkdir(parents=True)
    dead.write_text("{}", encoding="utf-8")

    result = resident.doctor(
        codex_home=codex_home,
        resident_root=resident_root,
        launch_agents_dir=launch_agents,
        platform="darwin",
        runner=lambda *_args, **_kwargs: Completed(),
    )

    assert result["healthy"] is False
    assert "resident session presence health state is stale" in result["issues"]
    assert "resident session presence is unhealthy: network unavailable" in result["issues"]
    assert "resident session presence has 1 pending record(s)" in result["issues"]
    assert "resident session presence has 1 dead-letter record(s)" in result["issues"]


def test_update_lock_reclaims_dead_process(tmp_path, monkeypatch):
    update_plugin = load_update_plugin()
    lock_path = tmp_path / "update.json.lock"
    lock_path.write_text("99999999", encoding="utf-8")
    monkeypatch.setattr(update_plugin, "process_is_running", lambda _pid: False)

    descriptor = update_plugin.acquire_lock(lock_path)
    try:
        assert lock_path.read_text(encoding="utf-8") == str(os.getpid())
    finally:
        update_plugin.release_lock(descriptor, lock_path)


def test_update_lock_preserves_live_process(tmp_path, monkeypatch):
    update_plugin = load_update_plugin()
    lock_path = tmp_path / "update.json.lock"
    lock_path.write_text("4242", encoding="utf-8")
    monkeypatch.setattr(update_plugin, "process_is_running", lambda pid: pid == 4242)

    with pytest.raises(FileExistsError):
        update_plugin.acquire_lock(lock_path)

    assert lock_path.read_text(encoding="utf-8") == "4242"


def test_hook_entrypoints_self_heal_resident_updater():
    for script_name in ("session_start.py", "pre_tool_use.py"):
        text = (ROOT / "plugins" / "linear-progress-sync" / "scripts" / script_name).read_text(encoding="utf-8")
        assert "ensure_resident_updater" in text


def test_setup_installs_resident_updater_without_teammate_followup(tmp_path):
    plan = linear_sync.setup_plan(plugin_repo_root=ROOT, target_repo_root=tmp_path)
    commands = "\n".join(plan["commands"])
    notes = "\n".join(plan["notes"])

    assert "install_resident_updater.py" in commands
    assert "updates run at login and every 30 minutes" in notes
    assert "no renewal thread" in notes


def test_install_codex_hooks_removes_legacy_plugin_hooks_and_preserves_user_hooks(tmp_path):
    codex_home = tmp_path / "codex-home"
    hooks_path = codex_home / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    linear_config = linear_sync.read_plugin_hooks_config(ROOT, "linear-progress-sync")
    logging_config = linear_sync.read_plugin_hooks_config(ROOT, "codex-session-logging")
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "/tmp/notify.sh"}]},
                        {"hooks": [{"type": "command", "command": "/tmp/codex-session-logging-monitor"}]},
                        linear_config["hooks"]["SessionStart"][0],
                        logging_config["hooks"]["SessionStart"][0],
                    ],
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "/tmp/notify.sh"}]},
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        "python3 ~/.codex/plugins/cache/coreedge-local/"
                                        "linear-progress-sync/0.3.1/scripts/stop_progress.py"
                                    ),
                                }
                            ]
                        },
                        logging_config["hooks"]["Stop"][0],
                    ],
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    result = linear_sync.install_codex_hooks(plugin_repo_root=ROOT, codex_home_path=codex_home)
    second = linear_sync.install_codex_hooks(plugin_repo_root=ROOT, codex_home_path=codex_home)
    installed = json.loads(hooks_path.read_text(encoding="utf-8"))

    assert result["changed"] is True
    assert second["changed"] is False
    assert installed["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "/tmp/notify.sh"
    installed_text = json.dumps(installed)
    assert "/tmp/codex-session-logging-monitor" in installed_text
    assert "plugins/cache/*/linear-progress-sync/*/scripts" not in installed_text
    assert "plugins/cache/*/codex-session-logging/*/scripts" not in installed_text
    assert {plugin["registration"] for plugin in result["plugins"]} == {"plugin-native"}


def test_install_codex_hooks_can_resolve_plugins_from_marketplace_cache_root(tmp_path):
    marketplace_root = tmp_path / "coreedge-local"
    write_minimal_plugin(
        marketplace_root,
        name="linear-progress-sync",
        version="0.2.8",
        hook_events=("SessionStart",),
    )
    write_minimal_plugin(
        marketplace_root,
        name="codex-session-logging",
        version="0.1.2",
        hook_events=("UserPromptSubmit",),
    )

    result = linear_sync.install_codex_hooks(
        plugin_repo_root=marketplace_root,
        codex_home_path=tmp_path / "codex-home",
    )

    assert result["changed"] is False
    assert {plugin["name"] for plugin in result["plugins"]} == {
        "linear-progress-sync",
        "codex-session-logging",
    }


def test_install_codex_hooks_can_resolve_plugins_from_versioned_cache_root(tmp_path):
    marketplace_root = tmp_path / "coreedge-local"
    write_minimal_plugin(
        marketplace_root / "linear-progress-sync",
        name="linear-progress-sync",
        directory_name="0.2.6",
        version="0.2.6",
        hook_events=("SessionStart",),
    )
    write_minimal_plugin(
        marketplace_root / "linear-progress-sync",
        name="linear-progress-sync",
        directory_name="0.2.8",
        version="0.2.8",
        hook_events=("SessionStart",),
    )
    write_minimal_plugin(
        marketplace_root / "codex-session-logging",
        name="codex-session-logging",
        directory_name="0.1.2",
        version="0.1.2",
        hook_events=("UserPromptSubmit",),
    )

    result = linear_sync.install_codex_hooks(
        plugin_repo_root=marketplace_root,
        codex_home_path=tmp_path / "codex-home",
    )

    assert result["changed"] is False
    assert any(
        plugin["source"].endswith("linear-progress-sync/0.2.8/hooks/hooks.json")
        for plugin in result["plugins"]
    )
    assert any(
        plugin["source"].endswith("codex-session-logging/0.1.2/hooks/hooks.json")
        for plugin in result["plugins"]
    )


def test_readmes_register_linear_mcp_before_linear_login():
    for rel in ("README.md", "plugins/linear-progress-sync/README.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert text.index("gh auth login") < text.index("python3 plugins/linear-progress-sync/scripts/setup.py")
        assert text.index("python3 plugins/linear-progress-sync/scripts/setup.py") < text.index(
            "codex mcp login linear"
        )
        assert "Run this once per teammate, not once per repo" in text
        assert "trust the Linear Progress Sync and Codex Session Logging hooks once" in text
        assert "saves it in `~/.codex/linear-sync/repos.json`" in text
        assert "update_plugin.py --force" in text
        assert "update_plugin.py --doctor" in text
        assert "`0.3.2`" in text
        assert "one-minute task-presence publisher" in text
        assert "renewal thread" in text
        assert "every 30 minutes" in text
        assert "historical-backfill protections" in text
        assert "LINEAR_SYNC_AUTO_UPDATE=0" in text
        assert "not a single plugin source" in text
        assert "Do not install the GitHub URL or repository root directly with `codex plugin add`" in text
        assert "file edits, write-like Bash commands, and branch creation wait" in text
        assert "Read-only and non-mutating Bash commands can run before kickoff" in text


def test_agent_install_contract_and_marketplace_defaults_are_explicit():
    agent_text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "not a single plugin source" in agent_text
    assert "do not run `codex plugin add` with the GitHub URL" in agent_text
    assert "python3 plugins/linear-progress-sync/scripts/setup.py" in agent_text

    marketplace = json.loads((ROOT / ".agents/plugins/marketplace.json").read_text(encoding="utf-8"))
    policies = {
        plugin["name"]: plugin["policy"]["installation"]
        for plugin in marketplace["plugins"]
    }
    assert policies["linear-progress-sync"] == "INSTALLED_BY_DEFAULT"
    assert policies["codex-session-logging"] == "INSTALLED_BY_DEFAULT"


def test_setup_run_step_does_not_treat_auth_failure_as_idempotent_success(monkeypatch):
    class Completed:
        returncode = 1
        stdout = ""
        stderr = "not configured already; run gh auth login"

    monkeypatch.setattr(linear_setup.shutil, "which", lambda executable: f"/usr/bin/{executable}")
    monkeypatch.setattr(linear_setup.subprocess, "run", lambda *args, **kwargs: Completed())

    result = linear_setup.run_step("gh auth status")

    assert result["ok"] is False
    assert "gh auth login" in result["message"]


def test_non_commit_events_are_discarded_without_linear_updates(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
    for event_type in ("file_change", "session_progress"):
        linear_sync.enqueue_event(
            event_type,
            {
                "id": f"evt-{event_type}",
                "branch": "nitish/cor-5-progress",
                "changed_files": ["training/tool_rlvr.py"],
            },
            root=tmp_path,
        )
    calls = []
    result = linear_sync.drain_once(root=tmp_path, executor=lambda *_: calls.append(True))
    state = linear_sync.read_state(tmp_path)

    assert result["skipped"] == 2
    assert calls == []
    assert set(state["processed_event_ids"]) == {"evt-file_change", "evt-session_progress"}
    assert not list((tmp_path / "events").glob("*.json"))


def test_post_commit_hook_returns_quickly():
    hook = linear_sync.build_post_commit_hook(ROOT / "plugins" / "linear-progress-sync")
    assert "drain_queue.py" in hook
    assert ") &" in hook
    assert "exit 0" in hook


def test_worker_failure_does_not_lose_queued_event(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    save_linear_user(tmp_path, monkeypatch)
    repo = init_git_repo(tmp_path / "repo", branch="nitish/cor-7-work")
    linear_sync.enqueue_event(
        "post_commit",
        {
            "id": "evt-fail",
            "branch": "nitish/cor-7-work",
            "commit_sha": "abc999",
            "commit_subject": "COR-7 useful work",
        },
        root=repo,
    )

    def failing_executor(prompt, event, inference):
        return linear_sync.WorkerResult(False, "temporary failure")

    result = linear_sync.drain_once(root=repo, executor=failing_executor)
    assert result["failed"] == 1
    assert list((state_dir / "events").glob("*.json"))


def test_plugin_hooks_do_not_use_repo_relative_script_paths():
    for rel in (
        "plugins/linear-progress-sync/hooks.json",
        "plugins/linear-progress-sync/hooks/hooks.json",
    ):
        text = (ROOT / rel).read_text()
        assert "./scripts/" not in text
        assert ".codex/plugins/cache" in text


def test_plugin_exposes_linear_start_command_and_pre_tool_guard_hook():
    manifest = json.loads((ROOT / "plugins/linear-progress-sync/.codex-plugin/plugin.json").read_text(encoding="utf-8"))
    command = ROOT / "plugins/linear-progress-sync/commands/linear-start.md"
    skill = ROOT / "plugins/linear-progress-sync/skills/linear-progress-sync/SKILL.md"
    session_start = ROOT / "plugins/linear-progress-sync/scripts/session_start.py"
    update_script = ROOT / "plugins/linear-progress-sync/scripts/update_plugin.py"
    assert command.exists()
    assert update_script.exists()
    command_text = command.read_text(encoding="utf-8")
    skill_text = skill.read_text(encoding="utf-8")
    session_start_text = session_start.read_text(encoding="utf-8")
    sync_command_text = (ROOT / "plugins/linear-progress-sync/commands/sync-linear-progress.md").read_text(
        encoding="utf-8"
    )
    assert "linear_start.py" in command_text
    assert "linear_start.py activate" in command_text
    assert "linear_start.py user-profile" in command_text
    assert "linear_start.py configure-user" in command_text
    assert "mcp__codex_apps__linear._list_users" in command_text
    assert "mcp__linear.list_users" in command_text
    assert "activation_command" in command_text
    assert "repo-binding" in command_text
    assert "configure-repo" in command_text
    assert "--disable-linear-sync" in command_text
    assert "mcp__linear." not in sync_command_text
    assert "mcp__codex_apps__linear._list_teams" in command_text
    assert "mcp__codex_apps__linear._list_projects" in command_text
    assert "mcp__codex_apps__linear._save_issue" in command_text
    assert "mcp__codex_apps__linear._get_issue" in command_text
    assert "mcp__codex_apps__linear._fetch" in command_text
    assert "mcp__codex_apps__linear._save_comment" in command_text
    assert "create a new issue automatically from the user's implementation request" in command_text
    assert "Do not ask the user for a Linear issue key" in command_text
    assert "do not stop, do not test write access" in command_text
    assert "activation_command" in command_text
    assert "create a new issue automatically from the user's implementation request" in skill_text.lower()
    assert "Do not ask the user for a Linear issue key" in skill_text
    assert "do not stop, do not test write access" in skill_text
    assert "mcp__linear.list_teams" in command_text
    assert "mcp__linear.list_projects" in command_text
    assert "mcp__linear.get_issue" in command_text
    assert "mcp__linear.save_issue" in command_text
    assert "mcp__linear.save_comment" in command_text
    assert "assignee: <stored Linear user name>" in command_text
    assert "Codex bot: <stored Linear user name> at <ISO-8601 UTC timestamp>" in command_text
    assert "short Linear aliases like `list_teams` or `list_projects`" in command_text
    assert "do not stop after listing projects" in command_text
    assert "do not answer with a code patch or say you are blocked" in command_text
    assert "do not answer with a code patch or say you are blocked" in skill_text
    assert "choose their Linear user from that list" in skill_text
    assert "choose the project from that list" in skill_text
    assert "--disable-linear-sync" in skill_text
    assert "update_plugin.py --force" in skill_text
    assert "update_plugin.py --doctor" in skill_text
    assert "renewal thread" in skill_text
    assert "LINEAR_SYNC_AUTO_UPDATE=0" in skill_text
    assert "configure-user" in skill_text
    assert "Do not create the Linear issue, branch, PR, or code changes until the chosen repo destination is saved" in skill_text
    assert "mcp__codex_apps__linear._list_comments" in sync_command_text
    assert "maybe_spawn_auto_update" in session_start_text
    assert manifest["hooks"] == "./hooks/hooks.json"
    for rel in (
        "plugins/linear-progress-sync/hooks.json",
        "plugins/linear-progress-sync/hooks/hooks.json",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert '"PreToolUse"' in text
        assert '"matcher"' not in text
        assert "pre_tool_use.py" in text
        assert "post_tool_use.py" in text


def test_dry_run_keeps_event_queued_and_unsynced(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    save_linear_user(tmp_path, monkeypatch)
    repo = init_git_repo(tmp_path / "repo", branch="nitish/cor-8-work")
    linear_sync.enqueue_event(
        "post_commit",
        {
            "id": "evt-dry",
            "branch": "nitish/cor-8-work",
            "commit_sha": "dry123",
            "commit_subject": "COR-8 useful work",
        },
        root=repo,
    )
    result = linear_sync.drain_once(root=repo, dry_run=True)
    state = linear_sync.read_state(repo)
    assert result["reviewed"] == 1
    assert "dry123" not in state["synced_commit_shas"]
    assert "evt-dry" not in state["processed_event_ids"]
    assert list((state_dir / "events").glob("*.json"))


def test_codex_prompt_requires_success_sentinel(tmp_path, monkeypatch):
    save_linear_user(tmp_path, monkeypatch)
    event = {"type": "post_commit", "commit_sha": "abc", "commit_subject": "COR-9 work"}
    inference = linear_sync.IssueInference("COR-9", 0.95, "branch")
    prompt = linear_sync.build_codex_prompt(event, inference)
    assert "LINEAR_SYNC_OK COR-9" in prompt
    assert "do not print LINEAR_SYNC_OK" in prompt


def test_codex_prompt_includes_linear_user_footer(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    linear_sync.save_linear_user_profile(linear_name="Arya G")
    event = {"type": "post_commit", "commit_sha": "abc", "commit_subject": "COR-9 work"}
    inference = linear_sync.IssueInference("COR-9", 0.95, "branch")

    prompt = linear_sync.build_codex_prompt(
        event,
        inference,
        now=datetime(2026, 7, 3, 18, 42, 10, tzinfo=timezone.utc),
    )

    assert "Codex bot: Arya G at 2026-07-03T18:42:10+00:00" in prompt


def test_codex_prompt_uses_one_timestamp_when_now_is_implicit(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    linear_sync.save_linear_user_profile(linear_name="Arya G")

    class FakeDateTime(datetime):
        calls = 0

        @classmethod
        def now(cls, tz=None):
            cls.calls += 1
            second = 10 if cls.calls == 1 else 11
            value = datetime(2026, 7, 3, 18, 42, second, tzinfo=timezone.utc)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr(linear_sync, "datetime", FakeDateTime)

    prompt = linear_sync.build_codex_prompt(
        {"type": "post_commit", "commit_sha": "abc", "commit_subject": "COR-9 work"},
        linear_sync.IssueInference("COR-9", 0.95, "branch"),
    )

    assert "Codex bot: Arya G at 2026-07-03T18:42:10+00:00" in prompt
    assert "Codex bot: Arya G at 2026-07-03T18:42:11+00:00" not in prompt


def test_foreground_prepare_returns_eligible_event_without_mutation(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    linear_sync.save_linear_user_profile(linear_name="Arya G")
    repo = init_git_repo(tmp_path / "repo", branch="nitish/cor-10-work")
    linear_sync.enqueue_event(
        "post_commit",
        {
            "id": "evt-fg",
            "branch": "nitish/cor-10-work",
            "commit_sha": "fg123",
            "short_sha": "fg123",
            "commit_subject": "COR-10 foreground sync",
            "changed_files": ["app.py"],
        },
        root=repo,
    )
    plan = linear_sync.foreground_sync_plan(root=repo)
    state = linear_sync.read_state(repo)
    assert plan["eligible"][0]["issue_key"] == "COR-10"
    assert "Codex progress update" in plan["eligible"][0]["comment_body"]
    assert "Codex bot: Arya G at " in plan["eligible"][0]["comment_body"]
    assert "foreground_sync.py ack" in plan["eligible"][0]["ack_command"]
    assert "evt-fg" not in state["processed_event_ids"]
    assert list((state_dir / "events").glob("*.json"))


def test_foreground_prepare_rejects_legacy_non_commit_event(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-10-commit-only")
    linear_sync.write_active_issue(
        active_payload(repo, issue_key="COR-10", branch="arya/cor-10-commit-only", pr_number=10),
        root=repo,
    )
    linear_sync.enqueue_event(
        "file_change",
        {
            "id": "evt-legacy-edit",
            "branch": "arya/cor-10-commit-only",
            "changed_files": ["app.py"],
        },
        root=repo,
    )

    plan = linear_sync.foreground_sync_plan(root=repo)

    assert plan["eligible"] == []
    assert plan["skipped"][0]["reason"] == "only post-commit events sync to Linear"


def test_foreground_ack_marks_synced_and_removes_event(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
    linear_sync.enqueue_event(
        "post_commit",
        {
            "id": "evt-ack",
            "branch": "nitish/cor-11-work",
            "commit_sha": "ack123",
            "commit_subject": "COR-11 foreground ack",
        },
        root=tmp_path,
    )
    result = linear_sync.ack_foreground_event("evt-ack", "COR-11", root=tmp_path)
    state = linear_sync.read_state(tmp_path)
    assert result["ok"] is True
    assert "evt-ack" in state["processed_event_ids"]
    assert "ack123" in state["synced_commit_shas"]
    assert not list((tmp_path / "events").glob("*.json"))


def test_foreground_skip_logs_noop_and_removes_event(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
    linear_sync.enqueue_event(
        "post_commit",
        {"id": "evt-skip", "branch": "nitish/cor-12-work", "commit_sha": "skip123"},
        root=tmp_path,
    )
    result = linear_sync.skip_foreground_event("evt-skip", reason="terminal issue", issue_key="COR-12", root=tmp_path)
    state = linear_sync.read_state(tmp_path)
    assert result["ok"] is True
    assert state["local_noops"][-1]["reason"] == "terminal issue"
    assert "evt-skip" in state["processed_event_ids"]
    assert not list((tmp_path / "events").glob("*.json"))


def test_commit_comment_uses_path_based_summary_and_grouped_areas(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    linear_sync.save_linear_user_profile(linear_name="Arya G")
    event = {
        "type": "post_commit",
        "short_sha": "b7753aa",
        "commit_subject": "Implement reusable usage dashboard",
        "changed_files": [
            "src/react/UsageDashboard.js",
            "src/react/UsageDashboard.css",
            "src/core/aggregate.js",
            "db/supabase/001_usage_dashboard.sql",
            "docs/adapter-contract.md",
            "test/aggregate.test.mjs",
        ],
    }
    comment = linear_sync.build_linear_comment(
        event,
        now=datetime(2026, 7, 3, 18, 42, 10, tzinfo=timezone.utc),
    )
    assert "Added or updated the React dashboard UI" in comment
    assert "Added or updated core usage aggregation" in comment
    assert "Database/Supabase schema" in comment
    assert "React dashboard UI: src/react/UsageDashboard.js" in comment
    assert "Tests: test/aggregate.test.mjs" in comment
    assert comment.endswith("Codex bot: Arya G at 2026-07-03T18:42:10+00:00")


def test_linear_comment_requires_user_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))

    with pytest.raises(ValueError, match="Linear user profile"):
        linear_sync.build_linear_comment(
            {"type": "post_commit", "short_sha": "b7753aa", "commit_subject": "Work"},
            now=datetime(2026, 7, 3, 18, 42, 10, tzinfo=timezone.utc),
        )


def test_changed_file_summary_falls_back_to_subject_when_empty():
    assert linear_sync.summarize_changed_files([], subject="Do useful work") == ["Do useful work"]
    assert linear_sync.changed_area_lines([]) == ["No changed-file list captured for this event"]
