from __future__ import annotations

import importlib.util
import subprocess
import sys
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


def test_active_linear_issue_state_round_trips_and_malformed_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
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
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-52-legacy")
    legacy = active_payload(repo, issue_key="COR-52", issue_title="Legacy active state", pr_number=52)
    legacy.pop("linear_linked_at")
    linear_sync.write_json_atomic(linear_sync.active_issue_path(root=repo), legacy)

    active = linear_sync.read_current_active_issue(root=repo)
    decision = linear_sync.pre_tool_guard_decision({"tool_name": "apply_patch"}, root=repo)

    assert active["issue_key"] == "COR-52"
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
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))

    write_decision = linear_sync.pre_tool_guard_decision({"tool_name": "apply_patch"}, root=tmp_path)
    branch_decision = linear_sync.pre_tool_guard_decision(
        {"tool_name": "Bash", "command": "git switch -c arya/new-work"},
        root=tmp_path,
    )

    assert write_decision.blocked is True
    assert branch_decision.blocked is True
    assert "Linear kickoff" in write_decision.message


def test_pre_tool_guard_blocks_writes_with_incomplete_active_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
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
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))

    commands = [
        "git switch --create arya/new-work",
        "git status --short\ngit switch -c arya/new-work",
        "git status --short\ngit checkout -b arya/new-work",
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
            root=tmp_path,
        )
        assert decision.blocked is True, command


def test_pre_tool_guard_blocks_unsafe_bash_writes_without_active_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
    commands = [
        "touch app.py",
        "sed -i '' 's/a/b/' app.py",
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
            root=tmp_path,
        )
        assert decision.blocked is True, command
        assert "Linear kickoff" in decision.message


def test_pre_tool_guard_blocks_shell_substitutions_without_active_state(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
    commands = [
        "git status $(touch app.py)",
        "git status `touch app.py`",
        "cat <(touch app.py)",
        'echo "don\'t $(touch app.py)"',
    ]

    for command in commands:
        decision = linear_sync.pre_tool_guard_decision(
            {"tool_name": "Bash", "command": command},
            root=tmp_path,
        )
        assert decision.blocked is True, command


def test_pre_tool_guard_allows_general_bash_with_active_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
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
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
    commands = [
        "git status & touch app.py",
        "rg foo README.md & touch app.py",
        "python3 plugins/linear-progress-sync/scripts/linear_start.py repo-binding --root . & touch app.py",
    ]

    for command in commands:
        decision = linear_sync.pre_tool_guard_decision(
            {"tool_name": "Bash", "command": command},
            root=tmp_path,
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
    repo = init_git_repo(tmp_path / "repo", branch="arya/cor-47-active")
    linear_sync.write_active_issue(active_payload(repo, issue_key="COR-47", issue_title="Active", pr_number=47), root=repo)

    decision = linear_sync.pre_tool_guard_decision(
        {"tool_name": "Bash", "command": "git switch -c arya/cor-48-other-work"},
        root=repo,
    )

    assert decision.blocked is True
    assert "only through the Linear kickoff workflow" in decision.message


def test_pre_tool_guard_blocks_chained_branch_creation_after_kickoff_helper(tmp_path, monkeypatch):
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(tmp_path))
    commands = [
        "python3 plugins/linear-progress-sync/scripts/linear_start.py repo-binding --root . && git switch -c arya/bypass",
        "echo linear_start.py; git switch -c arya/bypass",
        "/linear-start && git checkout -B arya/bypass",
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


def test_linear_start_returns_pending_state_without_writing_active_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    monkeypatch.setenv("LINEAR_SYNC_STATE_DIR", str(state_dir))
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


def test_setup_plan_is_global_by_default_and_does_not_install_repo_hook(tmp_path):
    plan = linear_sync.setup_plan(plugin_repo_root=ROOT, target_repo_root=tmp_path)
    commands = "\n".join(plan["commands"])
    notes = "\n".join(plan["notes"])

    assert "gh auth status" in commands
    assert "codex plugin marketplace add" in commands
    assert "codex plugin add linear-progress-sync@coreedge-local" in commands
    assert "codex mcp add linear --url https://mcp.linear.app/mcp" in commands
    assert "codex mcp login linear" not in commands
    assert "codex mcp login linear after setup" in notes
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


def test_readmes_register_linear_mcp_before_linear_login():
    for rel in ("README.md", "plugins/linear-progress-sync/README.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert text.index("gh auth login") < text.index("python3 plugins/linear-progress-sync/scripts/setup.py")
        assert text.index("python3 plugins/linear-progress-sync/scripts/setup.py") < text.index(
            "codex mcp login linear"
        )


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
    assert "linear_start.py activate" in command_text
    assert "activation_command" in command_text
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
