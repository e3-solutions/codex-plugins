from __future__ import annotations

import importlib.util
import hashlib
import json
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


def load_update_plugin():
    update_spec = importlib.util.spec_from_file_location("linear_update_plugin", UPDATE_PATH)
    update_plugin = importlib.util.module_from_spec(update_spec)
    assert update_spec.loader is not None
    update_spec.loader.exec_module(update_plugin)
    return update_plugin


def write_minimal_plugin(
    path: Path,
    *,
    version: str,
    name: str = "linear-progress-sync",
    hook_events: tuple[str, ...] | None = None,
) -> Path:
    plugin = path / name
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
                "policy": {"installation": "AVAILABLE"},
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


def init_git_repo(path: Path, branch: str = "arya/cor-1-work") -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "codex@example.test"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Codex Test"], cwd=path, check=True)
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


def test_pre_tool_guard_blocks_file_edits_without_active_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
    bind_linear_repo(tmp_path, tmp_path, monkeypatch)
    linear_sync.save_linear_user_profile(linear_name="Arya G")

    write_decision = linear_sync.pre_tool_guard_decision({"tool_name": "apply_patch"}, root=tmp_path)

    assert write_decision.blocked is True
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

    assert write_decision.blocked is True
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
    assert "before creating the issue or opening the PR" in write_decision.message
    assert "Do not ask the user for a Linear issue key" in write_decision.message
    assert "Create a new Linear issue from the user's implementation request" in write_decision.message


def test_pre_tool_guard_allows_non_e3_origin_without_linear_binding(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(config_dir))
    repo = init_git_repo(tmp_path / "repo", branch="arya/external-work")
    add_origin(repo, "https://github.com/other-org/example.git")
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


def test_pre_tool_guard_blocks_e3_origin_without_linear_binding(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(config_dir))
    repo = init_git_repo(tmp_path / "repo", branch="arya/e3-work")
    add_origin(repo, "git@github.com:e3-solutions/example.git")
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


def test_changed_paths_from_apply_patch_command_includes_added_files():
    paths = linear_sync.changed_paths_from_payload(
        {
            "tool_name": "apply_patch",
            "tool_input": {
                "command": """*** Begin Patch
*** Add File: src/new_module.py
+print("hello")
*** Update File: src/existing.py
@@
-old
+new
*** Delete File: obsolete.py
*** End Patch
""",
            },
        }
    )

    assert paths == ["obsolete.py", "src/existing.py", "src/new_module.py"]


def test_handle_post_tool_use_queues_apply_patch_added_file(tmp_path, monkeypatch):
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

    assert queued is not None
    assert queued["changed_files"] == ["src/new_module.py"]


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


def test_pre_tool_guard_allows_general_bash_without_active_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
    bind_linear_repo(tmp_path, tmp_path, monkeypatch)
    payloads = [
        {"tool_name": "Bash", "command": "python -m pytest -q"},
        {"tool_name": "Bash", "command": "touch app.py"},
        {"tool_name": "Bash", "command": "git switch -c arya/normal-work"},
        {"tool_name": "Bash"},
    ]

    for payload in payloads:
        decision = linear_sync.pre_tool_guard_decision(payload, root=tmp_path)
        assert decision.blocked is False, payload


def test_pre_tool_guard_blocks_unbound_repo_without_active_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    config_dir = tmp_path / "config"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(config_dir))
    repo = init_git_repo(tmp_path / "repo", branch="arya/no-linear-binding")
    linear_sync.save_linear_user_profile(linear_name="Arya G")

    write_decision = linear_sync.pre_tool_guard_decision({"tool_name": "apply_patch"}, root=repo)
    bash_decision = linear_sync.pre_tool_guard_decision(
        {"tool_name": "Bash", "command": "git switch -c arya/normal-work"},
        root=repo,
    )

    assert write_decision.blocked is True
    assert bash_decision.blocked is False
    assert write_decision.message.startswith("LINEAR DESTINATION REQUIRED")
    assert "No Linear team/project is saved for this repo" in write_decision.message


def test_pre_tool_guard_blocks_writes_with_incomplete_active_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
    save_linear_user(tmp_path, monkeypatch)
    linear_sync.write_json_atomic(
        tmp_path / "active.json",
        {"issue_key": "COR-40"},
    )

    decision = linear_sync.pre_tool_guard_decision({"tool_name": "apply_patch"}, root=tmp_path)

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


def test_pre_tool_guard_blocks_kickoff_until_global_linear_user_profile_is_saved(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))

    read_decision = linear_sync.pre_tool_guard_decision(
        {"tool_name": "Bash", "command": "git status --short"},
        root=tmp_path,
    )
    missing_user_decision = linear_sync.pre_tool_guard_decision(
        {"tool_name": "Bash", "command": "python3 plugins/linear-progress-sync/scripts/linear_start.py kickoff"},
        root=tmp_path,
    )
    profile_decision = linear_sync.pre_tool_guard_decision(
        {
            "tool_name": "Bash",
            "command": (
                "python3 plugins/linear-progress-sync/scripts/linear_start.py "
                "configure-user --linear-name 'Arya G'"
            ),
        },
        root=tmp_path,
    )
    linear_sync.save_linear_user_profile(linear_name="Arya G")
    ready_decision = linear_sync.pre_tool_guard_decision(
        {"tool_name": "Bash", "command": "python3 plugins/linear-progress-sync/scripts/linear_start.py kickoff"},
        root=tmp_path,
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

    event = linear_sync.enqueue_event("file_change", {"changed_files": ["app.py"]}, root=repo)

    assert event["issue_key"] == "COR-38"


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
    add_origin(repo, "git@github.com:e3-solutions/codex-plugins.git")

    assert linear_sync.repo_identity(repo) == "e3-solutions/codex-plugins"


def test_repo_linear_binding_round_trips_by_repo_identity(tmp_path, monkeypatch):
    config_dir = tmp_path / "config"
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(config_dir))
    repo = init_git_repo(tmp_path / "repo")
    add_origin(repo, "https://github.com/e3-solutions/codex-plugins.git")

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
    assert "install_codex_hooks.py" in commands
    assert "codex mcp add linear --url https://mcp.linear.app/mcp" in commands
    assert "codex mcp login linear" not in commands
    assert "codex mcp login linear after setup" in notes
    assert "review hooks" in notes
    assert "First use lists Linear users" in notes
    assert "First use in a repo lists Linear teams/projects" in notes
    assert "--disable-linear-sync" in notes
    assert "SessionStart" in notes
    assert "LINEAR_SYNC_AUTO_UPDATE=0" in notes
    assert "Codex file edits through apply_patch wait" in notes
    assert "general Bash commands are allowed" in notes
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
    assert "Before Linear kickoff, Codex file edits through apply_patch wait" in text
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
    assert "trust the Linear Progress Sync hooks once" in output
    assert "list Linear users/projects" in output
    assert "--disable-linear-sync" in output
    assert "LINEAR_SYNC_AUTO_UPDATE=0" in output
    assert "Before Linear kickoff, Codex file edits through apply_patch wait" in output
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
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    cache_root = tmp_path / "cache" / "coreedge-local"
    cache_parent = cache_root / "linear-progress-sync"
    current = write_minimal_plugin(
        cache_parent / "0.2.2",
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

    hooks = json.loads((tmp_path / "codex-home" / "hooks.json").read_text(encoding="utf-8"))
    hooks_text = json.dumps(hooks, sort_keys=True)
    assert result["updated"] is True
    assert {plugin["name"] for plugin in result["installed_plugins"]} == {"codex-session-logging"}
    assert second["updated"] is False
    assert second["installed_plugins"] == []
    assert (cache_root / "codex-session-logging" / "0.1.0" / ".codex-plugin" / "plugin.json").exists()
    assert not (cache_root / "internal-experiment").exists()
    assert "linear-progress-sync" in hooks_text
    assert "codex-session-logging" in hooks_text
    assert "internal-experiment" not in hooks_text
    assert hooks_text.count("codex-session-logging") == 2
    assert hooks_text.count("linear-progress-sync") == 2


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
        {"last_checked_at": "2026-07-03T18:00:00+00:00"},
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


def test_install_codex_hooks_merges_existing_user_hooks(tmp_path):
    codex_home = tmp_path / "codex-home"
    hooks_path = codex_home / "hooks.json"
    hooks_path.parent.mkdir(parents=True)
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "/tmp/notify.sh"}]},
                    ],
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "/tmp/notify.sh"}]},
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
    assert any("linear-progress-sync" in json.dumps(entry) for entry in installed["hooks"]["PreToolUse"])
    assert any("linear-progress-sync" in json.dumps(entry) for entry in installed["hooks"]["PostToolUse"])
    assert json.dumps(installed).count("pre_tool_use.py") == 1
    assert json.dumps(installed).count("post_tool_use.py") == 1


def test_readmes_register_linear_mcp_before_linear_login():
    for rel in ("README.md", "plugins/linear-progress-sync/README.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert text.index("gh auth login") < text.index("python3 plugins/linear-progress-sync/scripts/setup.py")
        assert text.index("python3 plugins/linear-progress-sync/scripts/setup.py") < text.index(
            "codex mcp login linear"
        )
        assert "Run this once per teammate, not once per repo" in text
        assert "trust the Linear Progress Sync hooks once" in text
        assert "saves it in `~/.codex/linear-sync/repos.json`" in text
        assert "update_plugin.py --force" in text
        assert "LINEAR_SYNC_AUTO_UPDATE=0" in text
        assert "Codex file edits through `apply_patch` wait" in text
        assert "General Bash commands are allowed before kickoff" in text


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


def test_session_progress_throttling_works(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
    state = linear_sync.default_state()
    now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    linear_sync.mark_session_progress(state, "COR-5", now=now)
    linear_sync.save_state(state, tmp_path)
    linear_sync.enqueue_event(
        "session_progress",
        {
            "id": "evt-session",
            "branch": "nitish/cor-5-progress",
            "changed_files": ["training/tool_rlvr.py"],
        },
        root=tmp_path,
    )
    calls = []
    result = linear_sync.drain_once(root=tmp_path, executor=lambda *_: calls.append(True), now=now)
    assert result["skipped"] == 1
    assert calls == []


def test_post_commit_hook_returns_quickly():
    hook = linear_sync.build_post_commit_hook(ROOT / "plugins" / "linear-progress-sync")
    assert "drain_queue.py" in hook
    assert ") &" in hook
    assert "exit 0" in hook


def test_worker_failure_does_not_lose_queued_event(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
    save_linear_user(tmp_path, monkeypatch)
    linear_sync.enqueue_event(
        "post_commit",
        {
            "id": "evt-fail",
            "branch": "nitish/cor-7-work",
            "commit_sha": "abc999",
            "commit_subject": "COR-7 useful work",
        },
        root=tmp_path,
    )

    def failing_executor(prompt, event, inference):
        return linear_sync.WorkerResult(False, "temporary failure")

    result = linear_sync.drain_once(root=tmp_path, executor=failing_executor)
    assert result["failed"] == 1
    assert list((tmp_path / "events").glob("*.json"))


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
    assert "LINEAR_SYNC_AUTO_UPDATE=0" in skill_text
    assert "configure-user" in skill_text
    assert "Before writing code, opening implementation changes, or applying Codex file edits" in skill_text
    assert "creating a branch" not in skill_text
    assert "Before the first write or branch creation" not in skill_text
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
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
    save_linear_user(tmp_path, monkeypatch)
    linear_sync.enqueue_event(
        "post_commit",
        {
            "id": "evt-dry",
            "branch": "nitish/cor-8-work",
            "commit_sha": "dry123",
            "commit_subject": "COR-8 useful work",
        },
        root=tmp_path,
    )
    result = linear_sync.drain_once(root=tmp_path, dry_run=True)
    state = linear_sync.read_state(tmp_path)
    assert result["reviewed"] == 1
    assert "dry123" not in state["synced_commit_shas"]
    assert "evt-dry" not in state["processed_event_ids"]
    assert list((tmp_path / "events").glob("*.json"))


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
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("LINEAR_SYNC_CONFIG_DIR", str(tmp_path / "config"))
    linear_sync.save_linear_user_profile(linear_name="Arya G")
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
        root=tmp_path,
    )
    plan = linear_sync.foreground_sync_plan(root=tmp_path)
    state = linear_sync.read_state(tmp_path)
    assert plan["eligible"][0]["issue_key"] == "COR-10"
    assert "Codex progress update" in plan["eligible"][0]["comment_body"]
    assert "Codex bot: Arya G at " in plan["eligible"][0]["comment_body"]
    assert "foreground_sync.py ack" in plan["eligible"][0]["ack_command"]
    assert "evt-fg" not in state["processed_event_ids"]
    assert list((tmp_path / "events").glob("*.json"))


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
