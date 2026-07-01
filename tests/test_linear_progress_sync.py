from __future__ import annotations

import importlib.util
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


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
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
    linear_sync.write_active_issue(
        {
            "issue_key": "COR-33",
            "issue_url": "https://linear.app/coreedge/issue/COR-33/test",
            "issue_title": "Active issue",
            "branch": "arya/cor-33-active-issue",
        },
        root=tmp_path,
    )
    event = {
        "branch": "arya/cor-999-wrong-branch",
        "commit_subject": "COR-888 wrong commit",
    }

    inference = linear_sync.infer_issue(event, root=tmp_path)

    assert inference.issue_key == "COR-33"
    assert inference.confidence == 1.0
    assert inference.reason == "active Linear issue state"


def test_explicit_event_issue_key_wins_over_active_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
    linear_sync.write_active_issue(
        {
            "issue_key": "COR-33",
            "issue_url": "https://linear.app/coreedge/issue/COR-33/test",
            "issue_title": "Active issue",
            "branch": "arya/cor-33-active-issue",
        },
        root=tmp_path,
    )

    inference = linear_sync.infer_issue(
        {
            "issue_key": "COR-99",
            "branch": "arya/cor-33-active-issue",
            "commit_subject": "COR-33 active issue",
        },
        root=tmp_path,
    )

    assert inference.issue_key == "COR-99"
    assert inference.confidence == 1.0
    assert inference.reason == "explicit event issue key"


def test_active_state_branch_mismatch_blocks_writes_and_does_not_infer_active(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-99-new-work")
    linear_sync.write_active_issue(
        {
            "issue_key": "COR-33",
            "issue_title": "Old work",
            "branch": "arya/cor-33-old-work",
        },
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
    assert inference.issue_key == "COR-99"
    assert inference.reason == "issue key found in branch name"


def test_active_linear_issue_state_round_trips_and_malformed_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
    payload = {
        "issue_key": "COR-34",
        "issue_url": "https://linear.app/coreedge/issue/COR-34/test",
        "issue_title": "Round trip",
        "branch": "arya/cor-34-round-trip",
        "pr_url": "https://github.com/e3-solutions/codex-plugins/pull/34",
    }

    linear_sync.write_active_issue(payload, root=tmp_path)

    assert linear_sync.read_active_issue(root=tmp_path)["issue_key"] == "COR-34"
    (tmp_path / "active.json").write_text("{not json", encoding="utf-8")
    assert linear_sync.read_active_issue(root=tmp_path) is None


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
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))

    write_decision = linear_sync.pre_tool_guard_decision({"tool_name": "apply_patch"}, root=tmp_path)
    branch_decision = linear_sync.pre_tool_guard_decision(
        {"tool_name": "Bash", "command": "git switch -c arya/new-work"},
        root=tmp_path,
    )

    assert write_decision.blocked is True
    assert branch_decision.blocked is True
    assert "Linear kickoff" in write_decision.message


def test_pre_tool_guard_blocks_long_form_branch_creation_without_active_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))

    commands = [
        "git switch --create arya/new-work",
        "git checkout --branch arya/new-work",
        "git checkout -B arya/new-work",
        "git switch --track origin/arya/new-work",
        "git checkout --track origin/arya/new-work",
        "git branch --track arya/new-work origin/main",
    ]

    for command in commands:
        decision = linear_sync.pre_tool_guard_decision(
            {"tool_name": "Bash", "command": command},
            root=tmp_path,
        )
        assert decision.blocked is True, command


def test_pre_tool_guard_allows_read_only_and_kickoff_commands_without_active_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))

    read_decision = linear_sync.pre_tool_guard_decision(
        {"tool_name": "Bash", "command": "git status --short"},
        root=tmp_path,
    )
    kickoff_decision = linear_sync.pre_tool_guard_decision(
        {"tool_name": "Bash", "command": "python3 plugins/linear-progress-sync/scripts/linear_start.py kickoff"},
        root=tmp_path,
    )

    assert read_decision.blocked is False
    assert kickoff_decision.blocked is False


def test_enqueue_event_carries_active_issue_key(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
    linear_sync.write_active_issue(
        {
            "issue_key": "COR-38",
            "issue_title": "Carry active issue",
            "branch": "arya/cor-38-carry-active-issue",
        },
        root=tmp_path,
    )

    event = linear_sync.enqueue_event("file_change", {"changed_files": ["app.py"]}, root=tmp_path)

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


def test_setup_plan_is_global_by_default_and_does_not_install_repo_hook(tmp_path):
    plan = linear_sync.setup_plan(plugin_repo_root=ROOT, target_repo_root=tmp_path)
    commands = "\n".join(plan["commands"])

    assert "gh auth status" in commands
    assert "codex plugin marketplace add" in commands
    assert "codex plugin add linear-progress-sync@coreedge-local" in commands
    assert "codex mcp add linear --url https://mcp.linear.app/mcp" in commands
    assert "codex mcp login linear" not in commands
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
    assert script.exists()
    text = script.read_text(encoding="utf-8")
    assert "--dry-run" in text
    assert "--with-git-hook" in text


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
    command = ROOT / "plugins/linear-progress-sync/commands/linear-start.md"
    assert command.exists()
    command_text = command.read_text(encoding="utf-8")
    sync_command_text = (ROOT / "plugins/linear-progress-sync/commands/sync-linear-progress.md").read_text(
        encoding="utf-8"
    )
    assert "linear_start.py" in command_text
    assert "repo-binding" in command_text
    assert "configure-repo" in command_text
    assert "mcp__linear." not in command_text
    assert "mcp__linear." not in sync_command_text
    assert "mcp__codex_apps__linear._list_teams" in command_text
    assert "mcp__codex_apps__linear._list_projects" in command_text
    assert "mcp__codex_apps__linear._save_issue" in command_text
    assert "mcp__codex_apps__linear._fetch" in command_text
    assert "mcp__codex_apps__linear._save_comment" in command_text
    assert "mcp__codex_apps__linear._list_comments" in sync_command_text
    for rel in (
        "plugins/linear-progress-sync/hooks.json",
        "plugins/linear-progress-sync/hooks/hooks.json",
    ):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert '"PreToolUse"' in text
        assert "pre_tool_use.py" in text


def test_dry_run_keeps_event_queued_and_unsynced(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
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


def test_codex_prompt_requires_success_sentinel():
    event = {"type": "post_commit", "commit_sha": "abc", "commit_subject": "COR-9 work"}
    inference = linear_sync.IssueInference("COR-9", 0.95, "branch")
    prompt = linear_sync.build_codex_prompt(event, inference)
    assert "LINEAR_SYNC_OK COR-9" in prompt
    assert "do not print LINEAR_SYNC_OK" in prompt


def test_foreground_prepare_returns_eligible_event_without_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
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


def test_commit_comment_uses_path_based_summary_and_grouped_areas():
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
    comment = linear_sync.build_linear_comment(event)
    assert "Added or updated the React dashboard UI" in comment
    assert "Added or updated core usage aggregation" in comment
    assert "Database/Supabase schema" in comment
    assert "React dashboard UI: src/react/UsageDashboard.js" in comment
    assert "Tests: test/aggregate.test.mjs" in comment


def test_changed_file_summary_falls_back_to_subject_when_empty():
    assert linear_sync.summarize_changed_files([], subject="Do useful work") == ["Do useful work"]
    assert linear_sync.changed_area_lines([]) == ["No changed-file list captured for this event"]
