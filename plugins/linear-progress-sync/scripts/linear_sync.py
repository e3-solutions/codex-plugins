#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

JsonDict = dict[str, Any]

ISSUE_RE = re.compile(r"(?<![A-Z0-9])([A-Z][A-Z0-9]+-\d+)(?![A-Z0-9])", re.IGNORECASE)
TERMINAL_STATUSES = {
    "done",
    "completed",
    "complete",
    "closed",
    "canceled",
    "cancelled",
    "wontfix",
    "won't fix",
    "duplicate",
}
SAFE_PROGRESS_STATUS = "In Progress"
STATE_DIR_ENV = "LINEAR_SYNC_STATE_DIR"
CONFIG_DIR_ENV = "LINEAR_SYNC_CONFIG_DIR"
DRY_RUN_ENV = "LINEAR_SYNC_DRY_RUN"
CODEX_COMMAND_ENV = "LINEAR_SYNC_CODEX_COMMAND"
THROTTLE_SECONDS = 30 * 60
LINEAR_MCP_URL = "https://mcp.linear.app/mcp"
IGNORED_FILE_PREFIXES = (
    ".codex/linear-sync/",
    ".git/",
)
IGNORED_FILE_NAMES = {
    ".DS_Store",
}


@dataclass(frozen=True)
class IssueInference:
    issue_key: str | None
    confidence: float
    reason: str
    status: str | None = None


@dataclass(frozen=True)
class WorkerResult:
    ok: bool
    message: str


@dataclass(frozen=True)
class PreToolGuardDecision:
    blocked: bool
    message: str = ""


def repo_root(start: str | Path | None = None) -> Path:
    cwd = Path(start or os.getcwd()).resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip()).resolve()
    return cwd


def state_dir(root: str | Path | None = None) -> Path:
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return repo_root(root) / ".codex" / "linear-sync"


def ensure_state(root: str | Path | None = None) -> Path:
    base = state_dir(root)
    (base / "events").mkdir(parents=True, exist_ok=True)
    (base / "logs").mkdir(parents=True, exist_ok=True)
    state_path = base / "state.json"
    if not state_path.exists():
        write_json_atomic(state_path, default_state())
    review_path = base / "review_queue.jsonl"
    if not review_path.exists():
        review_path.touch()
    return base


def default_state() -> JsonDict:
    return {
        "processed_event_ids": [],
        "synced_commit_shas": [],
        "issue_keys_by_commit": {},
        "last_session_progress_at": {},
        "stale_issue_cache": {
            "updated_at": None,
            "issues": [],
        },
        "failures": {},
        "local_noops": [],
    }


def read_state(root: str | Path | None = None) -> JsonDict:
    base = ensure_state(root)
    try:
        data = json.loads((base / "state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = default_state()
    merged = default_state()
    merged.update(data)
    return merged


def save_state(state: JsonDict, root: str | Path | None = None) -> None:
    base = ensure_state(root)
    write_json_atomic(base / "state.json", state)


def global_config_dir() -> Path:
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".codex" / "linear-sync"


def repo_bindings_path() -> Path:
    return global_config_dir() / "repos.json"


def read_repo_bindings() -> JsonDict:
    path = repo_bindings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"repos": {}}
    if not isinstance(data, dict):
        return {"repos": {}}
    repos = data.get("repos")
    if not isinstance(repos, dict):
        data["repos"] = {}
    return data


def save_repo_bindings(config: JsonDict) -> JsonDict:
    config.setdefault("repos", {})
    write_json_atomic(repo_bindings_path(), config)
    return config


def repo_identity(root: str | Path | None = None) -> str:
    remote = git_output(["remote", "get-url", "origin"], root=root)
    normalized = normalize_repo_remote(remote)
    return normalized or str(repo_root(root))


def normalize_repo_remote(remote: str) -> str | None:
    value = str(remote or "").strip()
    if not value:
        return None
    value = re.sub(r"\.git$", "", value)
    match = re.match(r"^(?:git@|ssh://git@|https?://)(?:[^/:]+)[:/](.+)$", value)
    if match:
        return match.group(1).strip("/")
    return value.strip("/")


def repo_binding_status(*, root: str | Path | None = None) -> JsonDict:
    repo = repo_identity(root)
    config = read_repo_bindings()
    repos = config.get("repos") if isinstance(config.get("repos"), dict) else {}
    binding = repos.get(repo) if isinstance(repos, dict) else None
    configured = (
        isinstance(binding, dict)
        and isinstance(binding.get("team"), str)
        and bool(binding.get("team", "").strip())
        and isinstance(binding.get("project"), str)
        and bool(binding.get("project", "").strip())
    )
    return {
        "repo": repo,
        "configured": configured,
        "binding": {"team": binding["team"], "project": binding["project"]} if configured else None,
        "config_path": str(repo_bindings_path()),
    }


def save_repo_linear_binding(
    *,
    team: str,
    project: str,
    root: str | Path | None = None,
) -> JsonDict:
    if not team.strip() or not project.strip():
        raise ValueError("repo Linear binding requires both team and project")
    repo = repo_identity(root)
    config = read_repo_bindings()
    repos = config.setdefault("repos", {})
    repos[repo] = {"team": team.strip(), "project": project.strip()}
    save_repo_bindings(config)
    return {
        "repo": repo,
        "binding": repos[repo],
        "config_path": str(repo_bindings_path()),
    }


def active_issue_path(root: str | Path | None = None) -> Path:
    return ensure_state(root) / "active.json"


def read_active_issue(*, root: str | Path | None = None) -> JsonDict | None:
    path = active_issue_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    issue_key = data.get("issue_key") or data.get("issue_identifier") or data.get("identifier")
    if not isinstance(issue_key, str) or not issue_key.strip():
        return None
    data["issue_key"] = issue_key.strip().upper()
    return data


def read_current_active_issue(*, root: str | Path | None = None) -> JsonDict | None:
    active = read_active_issue(root=root)
    if not active or active_issue_context_problem(active, root=root):
        return None
    return active


def active_issue_context_problem(active: JsonDict, *, root: str | Path | None = None) -> str | None:
    active_repo = str(active.get("repo") or "").strip()
    if not active_repo:
        return "active Linear issue state missing repo"
    current_repo = str(repo_root(root))
    if active_repo and normalize_path(active_repo) != normalize_path(current_repo):
        return f"active Linear issue repo {active_repo} does not match current repo {current_repo}"

    active_branch = str(active.get("branch") or "").strip()
    if not active_branch:
        return "active Linear issue state missing branch"
    branch = current_branch(root)
    if not branch:
        return "active Linear issue current branch could not be verified"
    if active_branch != branch:
        return f"active Linear issue branch {active_branch} does not match current branch {branch}"

    for field in ("issue_title", "issue_url", "pr_url"):
        value = active.get(field)
        if not isinstance(value, str) or not value.strip():
            return f"active Linear issue state missing {field}"

    pr_number = active.get("pr_number")
    if pr_number is None or str(pr_number).strip() == "":
        return "active Linear issue state missing pr_number"
    try:
        int(pr_number)
    except (TypeError, ValueError):
        return "active Linear issue state has invalid pr_number"

    return None


def normalize_path(path: str) -> str:
    return str(Path(path).expanduser().resolve())


def write_active_issue(payload: JsonDict, *, root: str | Path | None = None) -> JsonDict:
    issue_key = payload.get("issue_key") or payload.get("issue_identifier") or payload.get("identifier")
    if not isinstance(issue_key, str) or not issue_key.strip():
        raise ValueError("active Linear issue state requires issue_key")
    active = dict(payload)
    active["issue_key"] = issue_key.strip().upper()
    active.setdefault("created_at", now_iso())
    active.setdefault("repo", str(repo_root(root)))
    write_json_atomic(active_issue_path(root), active)
    return active


def write_json_atomic(path: Path, payload: JsonDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, payload: JsonDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def enqueue_event(event_type: str, payload: JsonDict | None = None, *, root: str | Path | None = None) -> JsonDict:
    base = ensure_state(root)
    event = dict(payload or {})
    event.setdefault("id", f"{event_type}-{uuid.uuid4().hex}")
    event.setdefault("type", event_type)
    event.setdefault("created_at", now_iso())
    event.setdefault("repo", str(repo_root(root)))
    event.setdefault("branch", current_branch(root))
    active = read_current_active_issue(root=root)
    if active:
        event.setdefault("issue_key", active["issue_key"])
        if active.get("pr_url"):
            event.setdefault("pr_url", active.get("pr_url"))
    event_path = base / "events" / f"{safe_file_name(event['id'])}.json"
    write_json_atomic(event_path, event)
    return event


def safe_file_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or uuid.uuid4().hex


def current_branch(root: str | Path | None = None) -> str | None:
    result = run_git(["rev-parse", "--abbrev-ref", "HEAD"], root=root)
    if result.returncode == 0:
        branch = result.stdout.strip()
        return None if branch == "HEAD" else branch
    return None


def run_git(args: list[str], *, root: str | Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root(root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def collect_commit_event(sha: str | None = None, *, root: str | Path | None = None) -> JsonDict:
    commit_sha = sha or git_output(["rev-parse", "HEAD"], root=root)
    subject = git_output(["show", "-s", "--format=%s", commit_sha], root=root)
    body = git_output(["show", "-s", "--format=%b", commit_sha], root=root)
    files_raw = git_output(["diff-tree", "--root", "--no-commit-id", "--name-only", "-r", commit_sha], root=root)
    if not files_raw.strip():
        files_raw = git_output(["show", "--name-only", "--format=", commit_sha], root=root)
    changed_files = [line for line in files_raw.splitlines() if line.strip()]
    return {
        "commit_sha": commit_sha,
        "short_sha": commit_sha[:7],
        "commit_subject": subject,
        "commit_body": body,
        "changed_files": changed_files,
        "diff_stat": git_output(["show", "--stat", "--format=", commit_sha], root=root),
        "summary_bullets": summarize_changed_files(changed_files, subject=subject),
        "branch": current_branch(root),
    }



def summarize_changed_files(changed_files: list[str], *, subject: str | None = None) -> list[str]:
    files = [normalize_repo_path(path) for path in changed_files if str(path).strip()]
    if not files:
        return [subject] if subject else ["Updated project files"]
    bullets: list[str] = []
    if any(path.startswith("src/react/") for path in files):
        bullets.append("Added or updated the React dashboard UI, styles, and type definitions.")
    if any(path.startswith("src/core/") for path in files):
        bullets.append("Added or updated core usage aggregation, contracts, and pricing logic.")
    if any(path.startswith("src/adapters/") for path in files):
        bullets.append("Added or updated adapter code for feeding resource usage data into the dashboard.")
    if any(path.startswith("db/") or path.startswith("supabase/") for path in files):
        bullets.append("Added or updated database/Supabase schema needed by the usage dashboard.")
    if any(path.startswith("test/") or path.startswith("tests/") for path in files):
        bullets.append("Added or updated tests for the changed usage/dashboard behavior.")
    if any(path.startswith("docs/") or path.lower().endswith("readme.md") for path in files):
        bullets.append("Updated documentation and integration notes for adopting the dashboard.")
    if any(path in {"package.json", "pyproject.toml", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"} for path in files):
        bullets.append("Updated package metadata or project dependencies for the new work.")
    if not bullets:
        top_dirs = sorted({path.split("/", 1)[0] for path in files})[:3]
        bullets.append(f"Updated {len(files)} file(s) across {', '.join(top_dirs)}.")
    return bullets[:4]


def changed_area_lines(changed_files: list[str], *, limit: int = 8) -> list[str]:
    files = [normalize_repo_path(path) for path in changed_files if str(path).strip()]
    if not files:
        return ["No changed-file list captured for this event"]
    groups: list[tuple[str, Callable[[str], bool]]] = [
        ("React dashboard UI", lambda p: p.startswith("src/react/")),
        ("Core usage aggregation/pricing", lambda p: p.startswith("src/core/")),
        ("Data adapters", lambda p: p.startswith("src/adapters/")),
        ("Database/Supabase schema", lambda p: p.startswith("db/") or p.startswith("supabase/")),
        ("Tests", lambda p: p.startswith("test/") or p.startswith("tests/")),
        ("Docs", lambda p: p.startswith("docs/") or p.lower().endswith("readme.md")),
        ("Package/API surface", lambda p: p in {"package.json", "pyproject.toml"} or p.startswith("src/index")),
    ]
    lines: list[str] = []
    used: set[str] = set()
    for label, predicate in groups:
        matched = [path for path in files if path not in used and predicate(path)]
        if matched:
            used.update(matched)
            sample = ", ".join(matched[:3])
            suffix = f" (+{len(matched) - 3} more)" if len(matched) > 3 else ""
            lines.append(f"{label}: {sample}{suffix}")
    for path in files:
        if path not in used:
            lines.append(path)
        if len(lines) >= limit:
            break
    return lines[:limit]

def git_output(args: list[str], *, root: str | Path | None = None) -> str:
    result = run_git(args, root=root)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def collect_today_commits(*, root: str | Path | None = None) -> list[JsonDict]:
    author = git_author(root=root)
    args = ["log", "--since=midnight", "--format=%H"]
    if author:
        args.append(f"--author={author}")
    shas = [line.strip() for line in git_output(args, root=root).splitlines() if line.strip()]
    return [collect_commit_event(sha, root=root) for sha in shas]


def git_author(*, root: str | Path | None = None) -> str | None:
    email = git_output(["config", "user.email"], root=root)
    if email:
        return email
    ident = git_output(["var", "GIT_AUTHOR_IDENT"], root=root)
    match = re.search(r"<([^>]+)>", ident)
    return match.group(1) if match else None


def infer_issue(event: JsonDict, state: JsonDict | None = None, *, root: str | Path | None = None) -> IssueInference:
    local_state = state or read_state(root)
    explicit_key = first_issue_key(str(event.get("issue_key") or ""))
    if explicit_key:
        return with_cached_status(IssueInference(explicit_key, 1.0, "explicit event issue key"), local_state)

    active = read_current_active_issue(root=root)
    if active:
        return IssueInference(
            active["issue_key"],
            1.0,
            "active Linear issue state",
            status=str(active.get("issue_status") or active.get("status") or "") or None,
        )

    branch = str(event.get("branch") or current_branch(root) or "")
    key = first_issue_key(branch)
    if key:
        return with_cached_status(IssueInference(key, 0.95, "issue key found in branch name"), local_state)

    commit_text = "\n".join(
        str(event.get(name) or "")
        for name in ("commit_subject", "commit_body", "message", "summary")
    )
    key = first_issue_key(commit_text)
    if key:
        return with_cached_status(IssueInference(key, 0.9, "issue key found in commit/message text"), local_state)

    pr_text = "\n".join(str(event.get(name) or "") for name in ("pr_title", "pr_body"))
    key = first_issue_key(pr_text)
    if key:
        return with_cached_status(IssueInference(key, 0.85, "issue key found in PR metadata"), local_state)

    fuzzy = infer_from_stale_cache(event, local_state)
    if fuzzy.issue_key:
        return fuzzy
    return IssueInference(None, 0.0, "no issue key or confident cached match")


def first_issue_key(text: str) -> str | None:
    match = ISSUE_RE.search(text or "")
    return match.group(1).upper() if match else None


def linear_issue_branch_name(issue: JsonDict) -> str | None:
    for key in ("branchName", "gitBranchName", "gitBranch", "branch_name"):
        value = issue.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("git", "branch"):
        value = issue.get(key)
        if isinstance(value, dict):
            nested_name = linear_issue_branch_name(value)
            if nested_name:
                return nested_name
    return None


def issue_identifier(issue: JsonDict) -> str:
    value = issue.get("identifier") or issue.get("issue_key") or issue.get("key") or issue.get("id")
    return str(value or "").strip().upper()


def issue_title(issue: JsonDict) -> str:
    return str(issue.get("title") or "").strip()


def slugify_title(title: str, *, max_length: int = 64) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)
    return (slug[:max_length].rstrip("-") or "work")


def fallback_branch_name(issue_key: str, title: str, *, prefix: str = "arya") -> str:
    return f"{prefix}/{issue_key.upper()}-{slugify_title(title)}"


def select_branch_name(issue: JsonDict, *, prefix: str = "arya") -> str:
    return linear_issue_branch_name(issue) or fallback_branch_name(
        issue_identifier(issue),
        issue_title(issue),
        prefix=prefix,
    )


def pr_title_for_issue(issue_key: str, title: str) -> str:
    return f"{issue_key.upper()}: {title.strip() or 'Linear work'}"


def pr_body_for_issue(issue_key: str, title: str, issue_url: str | None = None) -> str:
    lines = [
        f"Refs {issue_key.upper()}",
        "",
        f"Linear issue: {title.strip() or issue_key.upper()}",
    ]
    if issue_url:
        lines.append(str(issue_url))
    lines.extend(["", "This draft PR was created before implementation so Linear, branch, and PR stay linked."])
    return "\n".join(lines)


def linear_start_plan(
    *,
    issue_key: str,
    issue_title: str,
    issue_url: str | None,
    branch: str | None,
    team: str | None = None,
    project: str | None = None,
    root: str | Path | None = None,
) -> JsonDict:
    branch = branch or fallback_branch_name(issue_key, issue_title)
    title = pr_title_for_issue(issue_key, issue_title)
    body = pr_body_for_issue(issue_key, issue_title, issue_url)
    active_state = {
        "issue_key": issue_key.upper(),
        "issue_title": issue_title,
        "issue_url": issue_url,
        "branch": branch,
        "repo": str(repo_root(root)),
    }
    if team:
        active_state["team"] = team
    if project:
        active_state["project"] = project
    return {
        "active_state": active_state,
        "commands": [
            f"git switch {shlex.quote(branch)} || git switch -c {shlex.quote(branch)}",
            shlex.join(["git", "commit", "--allow-empty", "-m", f"chore: start {issue_key.upper()}"]),
            shlex.join(["git", "push", "-u", "origin", branch]),
            shlex.join(["gh", "pr", "create", "--draft", "--title", title, "--body", body]),
        ],
        "pr_title": title,
        "pr_body": body,
    }


def setup_plan(
    *,
    plugin_repo_root: str | Path,
    target_repo_root: str | Path | None = None,
    with_git_hook: bool = False,
) -> JsonDict:
    plugin_root = Path(plugin_repo_root).expanduser().resolve()
    target_root = Path(target_repo_root or os.getcwd()).expanduser().resolve()
    commands = [
        shlex.join(["gh", "auth", "status"]),
        shlex.join(["codex", "plugin", "marketplace", "add", str(plugin_root)]),
        shlex.join(["codex", "plugin", "add", "linear-progress-sync@coreedge-local"]),
        shlex.join(["codex", "mcp", "add", "linear", "--url", LINEAR_MCP_URL]),
    ]
    if with_git_hook:
        hook_script = plugin_root / "plugins" / "linear-progress-sync" / "scripts" / "install_git_hook.py"
        commands.append(shlex.join([sys.executable, str(hook_script), "--root", str(target_root)]))
    return {
        "commands": commands,
        "plugin_repo_root": str(plugin_root),
        "target_repo_root": str(target_root),
        "per_repo_setup_required": False,
        "optional_git_hook": with_git_hook,
        "notes": [
            "Default setup is user-level: plugin marketplace, plugin install, GitHub auth check, and Linear MCP registration.",
            "GitHub and Linear auth are manual prerequisites: run gh auth login and codex mcp login linear when needed.",
            "Per-repo Git hook setup is optional and only needed to sync commits made outside Codex.",
            "Start a new Codex thread after installing or updating the plugin so hooks and skills reload.",
        ],
    }


def run_linear_start(
    *,
    issue_key: str,
    issue_title: str,
    issue_url: str | None = None,
    branch: str | None = None,
    team: str | None = None,
    project: str | None = None,
    root: str | Path | None = None,
    dry_run: bool = False,
) -> JsonDict:
    plan = linear_start_plan(
        issue_key=issue_key,
        issue_title=issue_title,
        issue_url=issue_url,
        branch=branch,
        team=team,
        project=project,
        root=root,
    )
    if dry_run:
        return {**plan, "dry_run": True}

    target_branch = str(plan["active_state"]["branch"])
    if current_branch(root) != target_branch:
        switch_args = ["switch", target_branch] if local_branch_exists(target_branch, root=root) else ["switch", "-c", target_branch]
        require_success(run_git(switch_args, root=root), f"switch to {target_branch}")

    require_success(
        run_git(["commit", "--allow-empty", "-m", f"chore: start {issue_key.upper()}"], root=root),
        "create kickoff commit",
    )
    require_success(run_git(["push", "-u", "origin", target_branch], root=root), f"push {target_branch}")

    pr_result = run_local_command(
        ["gh", "pr", "create", "--draft", "--title", plan["pr_title"], "--body", plan["pr_body"]],
        root=root,
    )
    require_success(pr_result, "create draft pull request")
    pr_url = extract_pr_url(pr_result.stdout)
    if not pr_url:
        raise RuntimeError("Failed to create draft pull request: gh output did not include a pull request URL")
    pr_number = extract_pr_number(pr_url)
    if pr_number is None:
        raise RuntimeError("Failed to create draft pull request: pull request URL did not include a PR number")
    active_state = dict(plan["active_state"])
    active_state["pr_url"] = pr_url
    active_state["pr_number"] = pr_number
    active = write_active_issue(active_state, root=root)
    return {**plan, "active_state": active, "pr_url": pr_url}


def local_branch_exists(branch: str, *, root: str | Path | None = None) -> bool:
    return run_git(["rev-parse", "--verify", f"refs/heads/{branch}"], root=root).returncode == 0


def run_local_command(args: list[str], *, root: str | Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=repo_root(root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def require_success(result: subprocess.CompletedProcess[str], action: str) -> None:
    if result.returncode == 0:
        return
    output = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
    raise RuntimeError(f"Failed to {action}: {output or result.returncode}")


def extract_pr_url(output: str) -> str | None:
    match = re.search(r"https://github\.com/[^\s]+/pull/\d+", output or "")
    return match.group(0) if match else None


def extract_pr_number(url: str) -> int | None:
    match = re.search(r"/pull/(\d+)(?:$|[^\d])", url or "")
    return int(match.group(1)) if match else None


def infer_from_stale_cache(event: JsonDict, state: JsonDict) -> IssueInference:
    issues = ((state.get("stale_issue_cache") or {}).get("issues") or [])
    if not isinstance(issues, list):
        return IssueInference(None, 0.0, "stale issue cache missing")

    text = " ".join(
        [
            str(event.get("commit_subject") or ""),
            str(event.get("commit_body") or ""),
            str(event.get("last_assistant_message") or ""),
            " ".join(str(path) for path in event.get("changed_files") or []),
        ]
    ).lower()
    tokens = {token for token in re.split(r"[^a-z0-9_/-]+", text) if len(token) >= 4}
    best: tuple[float, JsonDict | None] = (0.0, None)
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        issue_text = " ".join(
            str(issue.get(name) or "")
            for name in ("id", "identifier", "key", "title", "description", "projectMilestone")
        ).lower()
        issue_tokens = {token for token in re.split(r"[^a-z0-9_/-]+", issue_text) if len(token) >= 4}
        if not issue_tokens:
            continue
        overlap = len(tokens & issue_tokens)
        score = min(0.75, overlap / max(6, len(issue_tokens) ** 0.5))
        if score > best[0]:
            best = (score, issue)

    if best[1] is None or best[0] < 0.5:
        return IssueInference(None, best[0], "cached issue fuzzy match below threshold")
    issue = best[1]
    issue_key = str(issue.get("identifier") or issue.get("id") or issue.get("key") or "")
    return IssueInference(issue_key.upper(), best[0], "fuzzy match from stale issue cache", status=issue_status(issue))


def with_cached_status(inference: IssueInference, state: JsonDict) -> IssueInference:
    status = cached_issue_status(inference.issue_key, state)
    return IssueInference(inference.issue_key, inference.confidence, inference.reason, status=status)


def cached_issue_status(issue_key: str | None, state: JsonDict) -> str | None:
    if not issue_key:
        return None
    issues = ((state.get("stale_issue_cache") or {}).get("issues") or [])
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        identifiers = {
            str(issue.get("id") or "").upper(),
            str(issue.get("identifier") or "").upper(),
            str(issue.get("key") or "").upper(),
        }
        if issue_key.upper() in identifiers:
            return issue_status(issue)
    return None


def issue_status(issue: JsonDict) -> str | None:
    value = issue.get("status") or issue.get("state") or issue.get("statusType")
    if isinstance(value, dict):
        value = value.get("name") or value.get("type")
    return str(value) if value else None


def is_terminal_status(status: str | None) -> bool:
    normalized = normalize_status(status)
    return normalized in TERMINAL_STATUSES


def is_safe_status_target(status: str | None) -> bool:
    return normalize_status(status) == normalize_status(SAFE_PROGRESS_STATUS)


def normalize_status(status: str | None) -> str:
    return re.sub(r"\s+", " ", str(status or "").strip().lower())


def already_synced_commit(state: JsonDict, sha: str | None, issue_key: str | None) -> bool:
    if not sha or not issue_key:
        return False
    issue_keys = state.get("issue_keys_by_commit", {}).get(sha, [])
    return issue_key.upper() in {str(key).upper() for key in issue_keys}


def mark_commit_synced(state: JsonDict, sha: str | None, issue_key: str | None) -> None:
    if not sha or not issue_key:
        return
    synced = set(state.setdefault("synced_commit_shas", []))
    synced.add(sha)
    state["synced_commit_shas"] = sorted(synced)
    by_commit = state.setdefault("issue_keys_by_commit", {})
    keys = {str(key).upper() for key in by_commit.get(sha, [])}
    keys.add(issue_key.upper())
    by_commit[sha] = sorted(keys)


def mark_processed(state: JsonDict, event_id: str | None) -> None:
    if not event_id:
        return
    processed = set(state.setdefault("processed_event_ids", []))
    processed.add(event_id)
    state["processed_event_ids"] = sorted(processed)


def increment_failure(state: JsonDict, event_id: str, message: str) -> None:
    failures = state.setdefault("failures", {})
    entry = failures.setdefault(event_id, {"count": 0, "last_error": None})
    entry["count"] = int(entry.get("count") or 0) + 1
    entry["last_error"] = message
    entry["last_failed_at"] = now_iso()


def should_throttle_session_progress(state: JsonDict, issue_key: str, *, now: datetime | None = None) -> bool:
    last_raw = (state.get("last_session_progress_at") or {}).get(issue_key.upper())
    if not last_raw:
        return False
    try:
        last = datetime.fromisoformat(last_raw)
    except ValueError:
        return False
    current = now or datetime.now(timezone.utc)
    return current - last < timedelta(seconds=THROTTLE_SECONDS)


def mark_session_progress(state: JsonDict, issue_key: str, *, now: datetime | None = None) -> None:
    timestamps = state.setdefault("last_session_progress_at", {})
    timestamps[issue_key.upper()] = (now or datetime.now(timezone.utc)).isoformat()


def write_review_queue(event: JsonDict, inference: IssueInference, reason: str, *, root: str | Path | None = None) -> None:
    base = ensure_state(root)
    append_jsonl(
        base / "review_queue.jsonl",
        {
            "created_at": now_iso(),
            "reason": reason,
            "inference": inference.__dict__,
            "event": event,
        },
    )


def log_noop(state: JsonDict, event: JsonDict, reason: str, inference: IssueInference | None = None) -> None:
    noops = state.setdefault("local_noops", [])
    noops.append(
        {
            "created_at": now_iso(),
            "event_id": event.get("id"),
            "event_type": event.get("type"),
            "issue_key": inference.issue_key if inference else None,
            "reason": reason,
        }
    )
    del noops[:-50]


def load_events(root: str | Path | None = None) -> list[tuple[Path, JsonDict]]:
    base = ensure_state(root)
    events: list[tuple[Path, JsonDict]] = []
    for path in sorted((base / "events").glob("*.json")):
        try:
            event = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(event, dict):
            events.append((path, event))
    return events


def drain_once(
    *,
    root: str | Path | None = None,
    dry_run: bool = False,
    executor: Callable[[str, JsonDict, IssueInference], WorkerResult] | None = None,
    now: datetime | None = None,
) -> JsonDict:
    local_state = read_state(root)
    processed = 0
    failed = 0
    reviewed = 0
    skipped = 0
    for path, event in load_events(root):
        event_id = str(event.get("id") or path.stem)
        if event_id in set(local_state.get("processed_event_ids") or []):
            safe_unlink(path)
            skipped += 1
            continue

        inference = infer_issue(event, local_state, root=root)
        if inference.confidence < 0.5 or not inference.issue_key:
            log_noop(local_state, event, "issue inference confidence below 0.5", inference)
            mark_processed(local_state, event_id)
            safe_unlink(path)
            skipped += 1
            continue
        if inference.confidence < 0.8:
            write_review_queue(event, inference, "issue inference confidence below Linear write threshold", root=root)
            mark_processed(local_state, event_id)
            safe_unlink(path)
            reviewed += 1
            continue
        if is_terminal_status(inference.status):
            log_noop(local_state, event, f"issue status is terminal: {inference.status}", inference)
            mark_processed(local_state, event_id)
            safe_unlink(path)
            skipped += 1
            continue
        if event.get("type") == "session_progress" and should_throttle_session_progress(
            local_state,
            inference.issue_key,
            now=now,
        ):
            log_noop(local_state, event, "session progress throttled", inference)
            mark_processed(local_state, event_id)
            safe_unlink(path)
            skipped += 1
            continue
        commit_sha = str(event.get("commit_sha") or "")
        if event.get("type") == "post_commit" and already_synced_commit(local_state, commit_sha, inference.issue_key):
            log_noop(local_state, event, "duplicate commit/issue update skipped", inference)
            mark_processed(local_state, event_id)
            safe_unlink(path)
            skipped += 1
            continue

        prompt = build_codex_prompt(event, inference)
        if dry_run or os.environ.get(DRY_RUN_ENV) == "1":
            write_review_queue(event, inference, "dry run: Linear write skipped; event left queued", root=root)
            reviewed += 1
            continue

        result = (executor or run_codex_update)(prompt, event, inference)
        if result.ok:
            mark_processed(local_state, event_id)
            if event.get("type") == "post_commit":
                mark_commit_synced(local_state, commit_sha, inference.issue_key)
            if event.get("type") == "session_progress":
                mark_session_progress(local_state, inference.issue_key, now=now)
            safe_unlink(path)
            processed += 1
        else:
            increment_failure(local_state, event_id, result.message)
            failed += 1
    save_state(local_state, root)
    return {
        "processed": processed,
        "reviewed": reviewed,
        "skipped": skipped,
        "failed": failed,
    }


def safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass



def build_linear_comment(event: JsonDict) -> str:
    if event.get("type") == "session_progress":
        summary = str(event.get("summary") or "Codex made meaningful local progress.")
        current_state = str(event.get("diff_stat") or "Local files changed; inspect repo diff for details.")
        return "\n".join(
            [
                "Codex session progress update",
                "",
                "Summary:",
                f"- {summary}",
                "",
                "Current state:",
                f"- {current_state}",
            ]
        )

    short_sha = str(event.get("short_sha") or str(event.get("commit_sha") or "")[:7] or "unknown")
    subject = str(event.get("commit_subject") or "Commit progress")
    changed = [str(path) for path in event.get("changed_files") or [] if str(path).strip()]
    bullets = [str(item) for item in event.get("summary_bullets") or [] if str(item).strip()]
    if not bullets:
        bullets = summarize_changed_files(changed, subject=subject)
    changed_lines = changed_area_lines(changed)
    return "\n".join(
        [
            "Codex progress update",
            "",
            f"Commit: `{short_sha}` — {subject}",
            "",
            "Summary:",
            *[f"- {bullet}" for bullet in bullets[:4]],
            "",
            "Changed areas:",
            *[f"- {line}" for line in changed_lines],
            "",
            "Status:",
            "- Work appears to be in progress.",
        ]
    )


def foreground_sync_plan(
    *,
    root: str | Path | None = None,
    limit: int = 5,
    now: datetime | None = None,
) -> JsonDict:
    local_state = read_state(root)
    eligible: list[JsonDict] = []
    held: list[JsonDict] = []
    skipped: list[JsonDict] = []
    for path, event in load_events(root):
        event_id = str(event.get("id") or path.stem)
        inference = infer_issue(event, local_state, root=root)
        item = {
            "event_id": event_id,
            "event_type": event.get("type"),
            "event": event,
            "inference": inference.__dict__,
        }
        if event_id in set(local_state.get("processed_event_ids") or []):
            skipped.append({**item, "reason": "already processed locally"})
            continue
        if inference.confidence < 0.5 or not inference.issue_key:
            skipped.append({**item, "reason": "issue inference confidence below 0.5"})
            continue
        if inference.confidence < 0.8:
            held.append({**item, "reason": "issue inference confidence below Linear write threshold"})
            continue
        if is_terminal_status(inference.status):
            skipped.append({**item, "reason": f"cached issue status is terminal: {inference.status}"})
            continue
        if event.get("type") == "session_progress" and should_throttle_session_progress(
            local_state,
            inference.issue_key,
            now=now,
        ):
            skipped.append({**item, "reason": "session progress throttled"})
            continue
        commit_sha = str(event.get("commit_sha") or "")
        if event.get("type") == "post_commit" and already_synced_commit(local_state, commit_sha, inference.issue_key):
            skipped.append({**item, "reason": "duplicate commit/issue update already synced locally"})
            continue
        eligible.append(
            {
                **item,
                "issue_key": inference.issue_key,
                "comment_body": build_linear_comment(event),
                "ack_command": foreground_ack_command(event, inference, root=root),
                "skip_command": foreground_skip_command(event_id, inference.issue_key, root=root),
            }
        )
        if len(eligible) >= limit:
            break
    return {
        "repo": str(repo_root(root)),
        "eligible": eligible,
        "held": held,
        "skipped": skipped,
        "instructions": [
            "For each eligible event, read the Linear issue and existing comments first.",
            "Never modify terminal Linear issues.",
            "If a matching comment already exists, run the ack command without adding a duplicate comment.",
            "After creating a comment, read comments back and run the ack command only when the comment is visible.",
            "If Linear write is denied or uncertain, leave the event queued.",
        ],
    }


def foreground_ack_command(event: JsonDict, inference: IssueInference, *, root: str | Path | None = None) -> str:
    script = Path(__file__).with_name("foreground_sync.py")
    parts = [
        sys.executable,
        str(script),
        "ack",
        "--root",
        str(repo_root(root)),
        "--event-id",
        str(event.get("id") or ""),
        "--issue-key",
        str(inference.issue_key or ""),
    ]
    if event.get("commit_sha"):
        parts.extend(["--commit-sha", str(event.get("commit_sha"))])
    return " ".join(shlex.quote(part) for part in parts)


def foreground_skip_command(event_id: str, issue_key: str | None, *, root: str | Path | None = None) -> str:
    script = Path(__file__).with_name("foreground_sync.py")
    parts = [
        sys.executable,
        str(script),
        "skip",
        "--root",
        str(repo_root(root)),
        "--event-id",
        event_id,
        "--reason",
        "foreground sync skipped",
    ]
    if issue_key:
        parts.extend(["--issue-key", issue_key])
    return " ".join(shlex.quote(part) for part in parts)


def ack_foreground_event(
    event_id: str,
    issue_key: str,
    *,
    commit_sha: str | None = None,
    root: str | Path | None = None,
    now: datetime | None = None,
) -> JsonDict:
    local_state = read_state(root)
    matched_path: Path | None = None
    matched_event: JsonDict | None = None
    for path, event in load_events(root):
        if str(event.get("id") or path.stem) == event_id:
            matched_path = path
            matched_event = event
            break
    if matched_event is None or matched_path is None:
        return {"ok": False, "event_id": event_id, "reason": "event not found"}
    mark_processed(local_state, event_id)
    if matched_event.get("type") == "post_commit":
        mark_commit_synced(local_state, commit_sha or matched_event.get("commit_sha"), issue_key)
    if matched_event.get("type") == "session_progress":
        mark_session_progress(local_state, issue_key, now=now)
    local_state.setdefault("failures", {}).pop(event_id, None)
    safe_unlink(matched_path)
    save_state(local_state, root)
    return {"ok": True, "event_id": event_id, "issue_key": issue_key, "action": "acked"}


def skip_foreground_event(
    event_id: str,
    *,
    reason: str,
    issue_key: str | None = None,
    root: str | Path | None = None,
) -> JsonDict:
    local_state = read_state(root)
    matched_path: Path | None = None
    matched_event: JsonDict | None = None
    for path, event in load_events(root):
        if str(event.get("id") or path.stem) == event_id:
            matched_path = path
            matched_event = event
            break
    if matched_event is None or matched_path is None:
        return {"ok": False, "event_id": event_id, "reason": "event not found"}
    inference = IssueInference(issue_key, 1.0 if issue_key else 0.0, "foreground skip")
    log_noop(local_state, matched_event, reason, inference)
    mark_processed(local_state, event_id)
    local_state.setdefault("failures", {}).pop(event_id, None)
    safe_unlink(matched_path)
    save_state(local_state, root)
    return {"ok": True, "event_id": event_id, "issue_key": issue_key, "action": "skipped", "reason": reason}

def build_codex_prompt(event: JsonDict, inference: IssueInference) -> str:
    event_json = json.dumps(event, indent=2, sort_keys=True)
    if event.get("type") == "session_progress":
        comment_template = """Codex session progress update

Summary:
- <1-3 bullets summarizing meaningful progress since last update>

Current state:
- <tests run / files changed / unresolved work if known>"""
    else:
        comment_template = """Codex progress update

Commit: `<short_sha>` — <commit subject>

Summary:
- <1-3 bullets summarizing actual change>

Changed areas:
- <top files/modules>

Status:
- Work appears to be in progress."""

    return f"""You are Linear Progress Sync running inside Codex.

Use the existing Linear MCP/app connection only. Do not use a custom Linear API client.

Hard safety rules:
- Never mark any issue Done, Completed, Closed, Canceled, or any terminal state.
- Only add a concise comment to issue {inference.issue_key}.
- Optionally move issue {inference.issue_key} to In Progress only if the current Linear state is non-terminal.
- If issue {inference.issue_key} is already terminal, do not modify Linear.
- Do not create duplicate comments if this exact commit/update already appears on the issue.
- After the Linear comment is actually created, print exactly: LINEAR_SYNC_OK {inference.issue_key}
- If you cannot access Linear tools or cannot confirm the comment was created, do not print LINEAR_SYNC_OK.

Issue inference:
- issue_key: {inference.issue_key}
- confidence: {inference.confidence:.2f}
- reason: {inference.reason}

Add a Linear comment using this shape:

```md
{comment_template}
```

Base the bullets only on this event payload:

```json
{event_json}
```
"""


def run_codex_update(prompt: str, event: JsonDict, inference: IssueInference) -> WorkerResult:
    command = os.environ.get(CODEX_COMMAND_ENV, "codex exec --ephemeral")
    argv = shlex.split(command)
    if not argv or shutil.which(argv[0]) is None:
        return WorkerResult(False, f"Codex CLI not available for {inference.issue_key}")
    result = subprocess.run(
        [*argv, prompt],
        cwd=event.get("repo") or os.getcwd(),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
        check=False,
    )
    output = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
    if result.returncode != 0:
        return WorkerResult(False, output or "codex exec failed")
    if "LINEAR_SYNC_OK" not in result.stdout:
        return WorkerResult(False, output or "codex exec did not confirm Linear update")
    return WorkerResult(True, result.stdout.strip())


def read_stdin_json() -> JsonDict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return payload if isinstance(payload, dict) else {"payload": payload}


def pre_tool_guard_decision(payload: JsonDict, *, root: str | Path | None = None) -> PreToolGuardDecision:
    tool = tool_name(payload)
    normalized_tool = tool.lower()
    requires_active_state = normalized_tool in {"apply_patch", "edit", "write"}
    if normalized_tool == "bash":
        command = tool_command(payload)
        if looks_like_branch_creation(command):
            requires_active_state = True
        elif is_linear_start_command(command):
            return PreToolGuardDecision(False)

    if requires_active_state:
        active = read_active_issue(root=root)
        if active:
            problem = active_issue_context_problem(active, root=root)
            if problem:
                return PreToolGuardDecision(True, linear_kickoff_required_message(problem))
            return PreToolGuardDecision(False)
        return PreToolGuardDecision(True, linear_kickoff_required_message())

    return PreToolGuardDecision(False)


def linear_kickoff_required_message(reason: str | None = None) -> str:
    prefix = f"{reason}. " if reason else ""
    return (
        f"{prefix}Linear kickoff is required before writing code or creating branches. "
        "Run the automatic Linear kickoff workflow first: confirm or create the Linear issue, "
        "create the Linear-named branch, push the empty kickoff commit, open the draft PR, "
        "link Linear and GitHub, then retry this tool call."
    )


def handle_post_tool_use(payload: JsonDict, *, root: str | Path | None = None) -> JsonDict | None:
    tool = tool_name(payload)
    if tool.lower() == "bash":
        command = tool_command(payload)
        if command and tool_success(payload) and looks_like_git_commit(command):
            event = collect_commit_event(root=root)
            event["source"] = "PostToolUse:Bash"
            queued = enqueue_event("post_commit", event, root=root)
            spawn_drain(root=root)
            return queued
    if tool in {"apply_patch", "Edit", "Write"} or tool.lower() in {"apply_patch", "edit", "write"}:
        paths = changed_paths_from_payload(payload)
        meaningful = [path for path in paths if meaningful_file(path)]
        if meaningful:
            return enqueue_event(
                "file_change",
                {
                    "source": f"PostToolUse:{tool}",
                    "changed_files": meaningful,
                    "summary": "Codex made meaningful local file edits.",
                },
                root=root,
            )
    return None


def tool_name(payload: JsonDict) -> str:
    for key in ("tool_name", "toolName", "name", "tool"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    tool = payload.get("tool")
    if isinstance(tool, dict) and isinstance(tool.get("name"), str):
        return tool["name"]
    return ""


def tool_command(payload: JsonDict) -> str:
    candidates = [
        payload.get("command"),
        payload.get("cmd"),
        nested(payload, "tool_input", "command"),
        nested(payload, "toolInput", "command"),
        nested(payload, "input", "command"),
        nested(payload, "arguments", "command"),
    ]
    for value in candidates:
        if isinstance(value, str) and value.strip():
            return value
    return ""


def nested(payload: JsonDict, *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def tool_success(payload: JsonDict) -> bool:
    for key in ("success", "ok"):
        if key in payload:
            return bool(payload[key])
    for key in ("exit_code", "exitCode", "returncode"):
        if key in payload:
            try:
                return int(payload[key]) == 0
            except (TypeError, ValueError):
                return False
    status = str(payload.get("status") or "").lower()
    if status:
        return status in {"success", "succeeded", "ok", "completed"}
    return True


def looks_like_git_commit(command: str) -> bool:
    return bool(re.search(r"(^|[;&|]\s*)git\s+commit(\s|$)", command))


def looks_like_branch_creation(command: str) -> bool:
    if not command:
        return False
    return any(git_tokens_create_branch(tokens) for tokens in shell_command_tokens(command))


def shell_command_tokens(command: str) -> list[list[str]]:
    tokens: list[list[str]] = []
    for segment in re.split(r"\s*(?:&&|\|\||;|\|)\s*", command):
        if not segment.strip():
            continue
        try:
            parsed = shlex.split(segment)
        except ValueError:
            continue
        if parsed:
            tokens.append(parsed)
    return tokens


def git_tokens_create_branch(tokens: list[str]) -> bool:
    try:
        git_index = tokens.index("git")
    except ValueError:
        return False
    args = skip_git_global_options(tokens[git_index + 1 :])
    if not args:
        return False
    command = args[0]
    rest = args[1:]

    if command == "switch":
        return has_long_option(rest, {"--create", "--force-create", "--track"}) or has_short_option(rest, {"c", "C", "t"})
    if command == "checkout":
        return has_long_option(rest, {"--branch", "--orphan", "--track"}) or has_short_option(rest, {"b", "B", "t"})
    if command == "branch":
        if has_long_option(rest, {"--copy", "--force-copy", "--track"}) or has_short_option(rest, {"c", "C", "t"}):
            return True
        return bool(rest and not rest[0].startswith("-"))
    if command == "worktree" and rest and rest[0] == "add":
        add_args = rest[1:]
        return has_long_option(add_args, {"--branch"}) or has_short_option(add_args, {"b", "B"})

    return False


def skip_git_global_options(args: list[str]) -> list[str]:
    index = 0
    value_options = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}
    while index < len(args):
        arg = args[index]
        if arg == "--":
            index += 1
            break
        if arg in value_options:
            index += 2
            continue
        if any(arg.startswith(f"{option}=") for option in value_options if option.startswith("--")):
            index += 1
            continue
        if arg.startswith("-"):
            index += 1
            continue
        break
    return args[index:]


def has_long_option(args: list[str], options: set[str]) -> bool:
    return any(arg == option or arg.startswith(f"{option}=") for arg in args for option in options)


def has_short_option(args: list[str], letters: set[str]) -> bool:
    for arg in args:
        if arg == "--":
            return False
        if arg.startswith("--"):
            continue
        if arg.startswith("-") and any(letter in arg[1:] for letter in letters):
            return True
    return False


def is_linear_start_command(command: str) -> bool:
    return "linear_start.py" in (command or "") or "/linear-start" in (command or "")


def changed_paths_from_payload(payload: JsonDict) -> list[str]:
    paths: list[str] = []
    for key in ("file_path", "path", "filename"):
        value = payload.get(key) or nested(payload, "tool_input", key) or nested(payload, "input", key)
        if isinstance(value, str):
            paths.append(value)
    for key in ("changed_files", "files"):
        value = payload.get(key)
        if isinstance(value, list):
            paths.extend(str(item) for item in value)
    return sorted({normalize_repo_path(path) for path in paths if path})


def normalize_repo_path(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def meaningful_file(path: str) -> bool:
    normalized = normalize_repo_path(path)
    if not normalized or normalized in IGNORED_FILE_NAMES:
        return False
    if any(normalized.startswith(prefix) for prefix in IGNORED_FILE_PREFIXES):
        return False
    if normalized.endswith((".pyc", ".log", ".tmp", ".swp")):
        return False
    return True


def session_progress_payload(hook_payload: JsonDict, *, root: str | Path | None = None) -> JsonDict | None:
    changed_files = current_changed_files(root=root)
    meaningful = [path for path in changed_files if meaningful_file(path)]
    if not meaningful:
        return None
    return {
        "source": "Stop",
        "branch": current_branch(root),
        "changed_files": meaningful[:50],
        "diff_stat": git_output(["diff", "--stat"], root=root),
        "last_assistant_message": last_assistant_message(hook_payload),
        "summary": "Codex made meaningful local progress without a commit in this turn.",
    }


def current_changed_files(*, root: str | Path | None = None) -> list[str]:
    tracked = git_output(["diff", "--name-only"], root=root).splitlines()
    staged = git_output(["diff", "--cached", "--name-only"], root=root).splitlines()
    untracked = git_output(["ls-files", "--others", "--exclude-standard"], root=root).splitlines()
    return sorted({normalize_repo_path(path) for path in [*tracked, *staged, *untracked] if path.strip()})


def last_assistant_message(payload: JsonDict) -> str | None:
    for key in ("last_assistant_message", "lastAssistantMessage", "assistant_message", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def spawn_drain(*, root: str | Path | None = None) -> None:
    script = Path(__file__).with_name("drain_queue.py")
    args = [sys.executable, str(script), "--once"]
    subprocess.Popen(
        args,
        cwd=repo_root(root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
    )


def build_post_commit_hook(plugin_root: Path) -> str:
    enqueue = plugin_root / "scripts" / "enqueue_event.py"
    drain = plugin_root / "scripts" / "drain_queue.py"
    return f"""#!/bin/sh
# Installed by linear-progress-sync. Keep this hook non-blocking.
PLUGIN_PYTHON="${{PYTHON:-python3}}"
"$PLUGIN_PYTHON" "{enqueue}" post_commit --from-git >/dev/null 2>&1 || true
(
  "$PLUGIN_PYTHON" "{drain}" --once >/dev/null 2>&1 || true
) &
exit 0
"""


def install_post_commit_hook(*, root: str | Path | None = None, force: bool = False) -> Path:
    plugin_root = Path(__file__).resolve().parents[1]
    hook_path = repo_root(root) / ".git" / "hooks" / "post-commit"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    if hook_path.exists() and not force:
        backup = hook_path.with_name(f"post-commit.linear-sync-backup.{int(time.time())}")
        hook_path.replace(backup)
    hook_path.write_text(build_post_commit_hook(plugin_root), encoding="utf-8")
    hook_path.chmod(0o755)
    return hook_path


def cli_root_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", help="Repository root. Defaults to git rev-parse from cwd.")
