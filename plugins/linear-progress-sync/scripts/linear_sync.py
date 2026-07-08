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
ALLOWED_GITHUB_ORG = "e3-solutions"
LINEAR_MCP_URL = "https://mcp.linear.app/mcp"
LINEAR_HOOK_EVENTS = ("SessionStart", "PreToolUse", "PostToolUse", "Stop")
IGNORED_FILE_PREFIXES = (
    ".codex/linear-sync/",
    ".git/",
)
IGNORED_FILE_NAMES = {
    ".DS_Store",
}
SHELL_COMMAND_PUNCTUATION = ";&|<>"


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


@dataclass(frozen=True)
class ActiveIssueRead:
    exists: bool
    active: JsonDict | None = None
    problem: str | None = None


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


def linear_user_profile_path() -> Path:
    return global_config_dir() / "user.json"


def read_linear_user_profile() -> JsonDict:
    path = linear_user_profile_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def linear_user_profile_status() -> JsonDict:
    data = read_linear_user_profile()
    linear_name = data.get("linear_name")
    configured = isinstance(linear_name, str) and bool(linear_name.strip())
    profile = {"linear_name": linear_name.strip()} if configured else None
    return {
        "configured": configured,
        "profile": profile,
        "config_path": str(linear_user_profile_path()),
    }


def save_linear_user_profile(*, linear_name: str) -> JsonDict:
    name = str(linear_name or "").strip()
    if not name:
        raise ValueError("Linear user profile requires linear_name")
    profile = {"linear_name": name}
    write_json_atomic(linear_user_profile_path(), profile)
    return {
        "configured": True,
        "profile": profile,
        "config_path": str(linear_user_profile_path()),
    }


def linear_user_name(*, default: str = "unknown Linear user") -> str:
    status = linear_user_profile_status()
    profile = status.get("profile") if status.get("configured") else None
    if isinstance(profile, dict):
        name = profile.get("linear_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return default


def linear_user_profile_configured() -> bool:
    return bool(linear_user_profile_status().get("configured"))


def require_linear_user_profile() -> JsonDict:
    status = linear_user_profile_status()
    if not status.get("configured"):
        raise ValueError("Linear user profile is required before Linear kickoff or attribution")
    profile = status.get("profile")
    return profile if isinstance(profile, dict) else {}


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
    return repo_remote_identity(root) or str(repo_root(root))


def repo_remote_identity(root: str | Path | None = None) -> str | None:
    remote = git_output(["remote", "get-url", "origin"], root=root)
    return normalize_repo_remote(remote)


def normalize_repo_remote(remote: str) -> str | None:
    value = str(remote or "").strip()
    if not value:
        return None
    value = re.sub(r"\.git$", "", value)
    match = re.match(r"^(?:git@|ssh://git@|https?://)(?:[^/:]+)[:/](.+)$", value)
    if match:
        return match.group(1).strip("/")
    return value.strip("/")


def repo_in_allowed_org(root: str | Path | None = None) -> bool:
    remote = repo_remote_identity(root)
    if not remote:
        return True
    return remote == ALLOWED_GITHUB_ORG or remote.startswith(f"{ALLOWED_GITHUB_ORG}/")


def repo_binding_status(*, root: str | Path | None = None) -> JsonDict:
    repo = repo_identity(root)
    in_allowed_org = repo_in_allowed_org(root=root)
    config = read_repo_bindings()
    repos = config.get("repos") if isinstance(config.get("repos"), dict) else {}
    binding = repos.get(repo) if isinstance(repos, dict) else None
    disabled = isinstance(binding, dict) and binding.get("disabled") is True
    scope_disabled = not in_allowed_org
    configured = (
        isinstance(binding, dict)
        and not disabled
        and not scope_disabled
        and isinstance(binding.get("team"), str)
        and bool(binding.get("team", "").strip())
        and isinstance(binding.get("project"), str)
        and bool(binding.get("project", "").strip())
    )
    if scope_disabled:
        returned_binding = {
            "disabled": True,
            "reason": f"outside {ALLOWED_GITHUB_ORG} GitHub org",
        }
    elif disabled:
        returned_binding = {
            "disabled": True,
            "reason": str(binding.get("reason") or "").strip(),
        }
    else:
        returned_binding = {"team": binding["team"], "project": binding["project"]} if configured else None
    return {
        "repo": repo,
        "configured": configured,
        "disabled": disabled or scope_disabled,
        "scope_disabled": scope_disabled,
        "binding": returned_binding,
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


def save_repo_linear_opt_out(
    *,
    reason: str | None = None,
    root: str | Path | None = None,
) -> JsonDict:
    repo = repo_identity(root)
    config = read_repo_bindings()
    repos = config.setdefault("repos", {})
    binding: JsonDict = {"disabled": True}
    if reason and str(reason).strip():
        binding["reason"] = str(reason).strip()
    repos[repo] = binding
    save_repo_bindings(config)
    return {
        "repo": repo,
        "binding": repos[repo],
        "config_path": str(repo_bindings_path()),
    }


def active_issue_path(root: str | Path | None = None) -> Path:
    return ensure_state(root) / "active.json"


def load_active_issue(*, root: str | Path | None = None) -> ActiveIssueRead:
    path = active_issue_path(root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ActiveIssueRead(False)
    except json.JSONDecodeError:
        return ActiveIssueRead(True, problem="active Linear issue state is malformed")
    except OSError as exc:
        return ActiveIssueRead(True, problem=f"active Linear issue state could not be read: {exc}")
    if not isinstance(data, dict):
        return ActiveIssueRead(True, problem="active Linear issue state must be a JSON object")
    issue_key = data.get("issue_key") or data.get("issue_identifier") or data.get("identifier")
    if not isinstance(issue_key, str) or not issue_key.strip():
        return ActiveIssueRead(True, problem="active Linear issue state missing issue_key")
    data["issue_key"] = issue_key.strip().upper()
    return ActiveIssueRead(True, active=data)


def read_active_issue(*, root: str | Path | None = None) -> JsonDict | None:
    loaded = load_active_issue(root=root)
    if loaded.problem:
        return None
    return loaded.active


def active_issue_fail_closed_problem(*, root: str | Path | None = None) -> str | None:
    loaded = load_active_issue(root=root)
    if not loaded.exists:
        return None
    if loaded.problem:
        return loaded.problem
    if not loaded.active:
        return "active Linear issue state could not be read"
    return active_issue_context_problem(loaded.active, root=root)


def active_issue_write_problem(*, root: str | Path | None = None) -> str | None:
    loaded = load_active_issue(root=root)
    if not loaded.exists:
        return "Linear kickoff has not created active issue state"
    if loaded.problem:
        return loaded.problem
    if not loaded.active:
        return "active Linear issue state could not be read"
    return active_issue_context_problem(loaded.active, root=root)


def active_issue_schema_problem(active: JsonDict, *, require_linear_linked_at: bool = False) -> str | None:
    issue_key = active.get("issue_key") or active.get("issue_identifier") or active.get("identifier")
    if not isinstance(issue_key, str) or not issue_key.strip():
        return "active Linear issue state missing issue_key"

    for field in ("repo", "branch", "issue_title", "issue_url", "pr_url"):
        value = active.get(field)
        if not isinstance(value, str) or not value.strip():
            return f"active Linear issue state missing {field}"
    if require_linear_linked_at:
        value = active.get("linear_linked_at")
        if not isinstance(value, str) or not value.strip():
            return "active Linear issue state missing linear_linked_at"

    pr_number = active.get("pr_number")
    if pr_number is None or str(pr_number).strip() == "":
        return "active Linear issue state missing pr_number"
    try:
        int(pr_number)
    except (TypeError, ValueError):
        return "active Linear issue state has invalid pr_number"

    return None


def read_current_active_issue(*, root: str | Path | None = None) -> JsonDict | None:
    loaded = load_active_issue(root=root)
    if loaded.problem or not loaded.active or active_issue_context_problem(loaded.active, root=root):
        return None
    return loaded.active


def active_issue_context_problem(active: JsonDict, *, root: str | Path | None = None) -> str | None:
    schema_problem = active_issue_schema_problem(active)
    if schema_problem:
        return schema_problem

    active_repo = str(active.get("repo") or "").strip()
    current_repo = str(repo_root(root))
    if active_repo and normalize_path(active_repo) != normalize_path(current_repo):
        return f"active Linear issue repo {active_repo} does not match current repo {current_repo}"

    active_branch = str(active.get("branch") or "").strip()
    branch = current_branch(root)
    if not branch:
        return "active Linear issue current branch could not be verified"
    if active_branch != branch:
        return f"active Linear issue branch {active_branch} does not match current branch {branch}"

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
    problem = active_issue_schema_problem(active, require_linear_linked_at=True)
    if problem:
        raise ValueError(problem)
    write_json_atomic(active_issue_path(root), active)
    return active


def write_json_atomic(path: Path, payload: JsonDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def codex_home() -> Path:
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".codex"


def codex_hooks_path(*, home: str | Path | None = None) -> Path:
    base = Path(home).expanduser().resolve() if home else codex_home()
    return base / "hooks.json"


def read_json_object(path: Path) -> JsonDict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON") from exc
    except OSError as exc:
        raise ValueError(f"{path} could not be read: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def linear_plugin_root(plugin_repo_root: str | Path) -> Path:
    root = Path(plugin_repo_root).expanduser().resolve()
    if (root / ".codex-plugin" / "plugin.json").exists():
        return root
    return root / "plugins" / "linear-progress-sync"


def linear_hooks_config_path(plugin_repo_root: str | Path) -> Path:
    plugin_root = linear_plugin_root(plugin_repo_root)
    for candidate in (plugin_root / "hooks" / "hooks.json", plugin_root / "hooks.json"):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(f"missing Linear hook config under {plugin_root}")


def read_linear_hooks_config(plugin_repo_root: str | Path) -> JsonDict:
    path = linear_hooks_config_path(plugin_repo_root)
    config = read_json_object(path)
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        raise ValueError(f"{path} missing hooks object")
    return config


def hook_entry_mentions_linear_sync(entry: Any) -> bool:
    try:
        text = json.dumps(entry, sort_keys=True)
    except TypeError:
        text = str(entry)
    return "linear-progress-sync" in text


def merge_linear_hooks(existing: JsonDict, linear_config: JsonDict) -> JsonDict:
    merged = json.loads(json.dumps(existing)) if existing else {}
    merged_hooks = merged.setdefault("hooks", {})
    if not isinstance(merged_hooks, dict):
        raise ValueError("existing hooks.json field `hooks` must be an object")

    linear_hooks = linear_config.get("hooks")
    if not isinstance(linear_hooks, dict):
        raise ValueError("Linear hook config missing hooks object")

    for event in LINEAR_HOOK_EVENTS:
        incoming = linear_hooks.get(event)
        if incoming is None:
            continue
        if not isinstance(incoming, list):
            raise ValueError(f"Linear hook event {event} must be a list")
        current = merged_hooks.get(event, [])
        if current is None:
            current = []
        if not isinstance(current, list):
            raise ValueError(f"existing hooks event {event} must be a list")
        kept = [entry for entry in current if not hook_entry_mentions_linear_sync(entry)]
        merged_hooks[event] = kept + json.loads(json.dumps(incoming))

    return merged


def install_codex_hooks(
    *,
    plugin_repo_root: str | Path,
    codex_home_path: str | Path | None = None,
    dry_run: bool = False,
) -> JsonDict:
    hooks_path = codex_hooks_path(home=codex_home_path)
    existing = read_json_object(hooks_path)
    linear_config = read_linear_hooks_config(plugin_repo_root)
    merged = merge_linear_hooks(existing, linear_config)
    changed = merged != existing
    if changed and not dry_run:
        write_json_atomic(hooks_path, merged)
    return {
        "ok": True,
        "changed": changed,
        "dry_run": dry_run,
        "path": str(hooks_path),
        "source": str(linear_hooks_config_path(plugin_repo_root)),
        "events": [event for event in LINEAR_HOOK_EVENTS if event in (linear_config.get("hooks") or {})],
    }


def append_jsonl(path: Path, payload: JsonDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_timestamp(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat(timespec="seconds")


def codex_attribution_footer(*, now: datetime | None = None) -> str:
    profile = require_linear_user_profile()
    return f"Codex bot: {profile['linear_name']} at {utc_timestamp(now)}"


def with_codex_attribution(body: str, *, now: datetime | None = None) -> str:
    return "\n".join([body.rstrip(), "", codex_attribution_footer(now=now)])


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
    loaded_active = load_active_issue(root=root)
    if loaded_active.exists:
        if loaded_active.problem:
            return IssueInference(None, 0.0, loaded_active.problem)
        active = loaded_active.active
        problem = active_issue_context_problem(active, root=root) if active else "active Linear issue state could not be read"
        if problem:
            return IssueInference(None, 0.0, problem)
        return IssueInference(
            active["issue_key"],
            1.0,
            "active Linear issue state",
            status=str(active.get("issue_status") or active.get("status") or "") or None,
        )

    explicit_key = first_issue_key(str(event.get("issue_key") or ""))
    if explicit_key:
        return with_cached_status(IssueInference(explicit_key, 1.0, "explicit event issue key"), local_state)

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
    if not issue_url or not str(issue_url).strip():
        raise ValueError("Linear kickoff requires issue_url before creating GitHub or Git state")
    branch = branch or fallback_branch_name(issue_key, issue_title)
    title = pr_title_for_issue(issue_key, issue_title)
    body = pr_body_for_issue(issue_key, issue_title, str(issue_url).strip())
    active_state = {
        "issue_key": issue_key.upper(),
        "issue_title": issue_title,
        "issue_url": str(issue_url).strip(),
        "branch": branch,
        "repo": str(repo_root(root)),
    }
    user_name = linear_user_name(default="")
    if user_name:
        active_state["linear_user_name"] = user_name
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
        shlex.join(
            [
                sys.executable,
                str(plugin_root / "plugins" / "linear-progress-sync" / "scripts" / "install_codex_hooks.py"),
                "--plugin-root",
                str(plugin_root),
            ]
        ),
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
            "Default setup is user-level: plugin marketplace, plugin install, global Codex hooks, GitHub auth check, and Linear MCP registration.",
            "GitHub auth is a manual prerequisite: run gh auth login when needed.",
            "Linear auth is manual after setup registers the MCP server: run codex mcp login linear after setup when needed.",
            "If Codex asks to review hooks, trust the Linear Progress Sync hooks once so automatic kickoff can run.",
            "First use lists Linear users, asks which user to save, and stores it in ~/.codex/linear-sync/user.json for all repos.",
            "First use in a repo lists Linear teams/projects, asks which project to save, and stores it in ~/.codex/linear-sync/repos.json.",
            "Repos that should not use Linear sync can be opted out with linear_start.py configure-repo --disable-linear-sync.",
            "Installed plugins check for updates on every SessionStart; set LINEAR_SYNC_AUTO_UPDATE=0 to disable.",
            "Before kickoff, Codex file edits through apply_patch wait for active Linear state; general Bash commands are allowed.",
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
    require_linear_user_profile()
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

    require_clean_worktree(root=root)
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
    pending_active_state = dict(plan["active_state"])
    pending_active_state["pr_url"] = pr_url
    pending_active_state["pr_number"] = pr_number
    return {
        **plan,
        "pending_active_state": pending_active_state,
        "pr_url": pr_url,
        "activation_command": linear_start_activation_command(pending_active_state, root=root),
    }


def activate_linear_start(
    *,
    issue_key: str,
    issue_title: str,
    issue_url: str,
    branch: str,
    pr_url: str,
    pr_number: int | str,
    team: str | None = None,
    project: str | None = None,
    root: str | Path | None = None,
    linked_at: str | None = None,
) -> JsonDict:
    active_state: JsonDict = {
        "issue_key": issue_key.upper(),
        "issue_title": issue_title,
        "issue_url": issue_url,
        "branch": branch,
        "repo": str(repo_root(root)),
        "pr_url": pr_url,
        "pr_number": int(pr_number),
        "linear_linked_at": linked_at or now_iso(),
    }
    user_name = linear_user_name(default="")
    if user_name:
        active_state["linear_user_name"] = user_name
    if team:
        active_state["team"] = team
    if project:
        active_state["project"] = project
    return write_active_issue(active_state, root=root)


def linear_start_activation_command(active_state: JsonDict, *, root: str | Path | None = None) -> str:
    script = Path(__file__).with_name("linear_start.py")
    parts = [
        sys.executable,
        str(script),
        "activate",
        "--root",
        str(repo_root(root)),
        "--issue-key",
        str(active_state.get("issue_key") or ""),
        "--issue-title",
        str(active_state.get("issue_title") or ""),
        "--issue-url",
        str(active_state.get("issue_url") or ""),
        "--branch",
        str(active_state.get("branch") or ""),
        "--pr-url",
        str(active_state.get("pr_url") or ""),
        "--pr-number",
        str(active_state.get("pr_number") or ""),
    ]
    for key, flag in (("team", "--team"), ("project", "--project")):
        value = active_state.get(key)
        if value:
            parts.extend([flag, str(value)])
    return " ".join(shlex.quote(part) for part in parts)


def local_branch_exists(branch: str, *, root: str | Path | None = None) -> bool:
    return run_git(["rev-parse", "--verify", f"refs/heads/{branch}"], root=root).returncode == 0


def require_clean_worktree(*, root: str | Path | None = None) -> None:
    dirty = worktree_status_entries(root=root)
    if not dirty:
        return
    sample = ", ".join(dirty[:5])
    suffix = f" (+{len(dirty) - 5} more)" if len(dirty) > 5 else ""
    raise RuntimeError(
        "Linear kickoff requires a clean worktree before creating the kickoff branch/commit; "
        f"commit, stash, or reset these changes first: {sample}{suffix}"
    )


def worktree_status_entries(*, root: str | Path | None = None) -> list[str]:
    result = run_git(["status", "--porcelain", "--untracked-files=all"], root=root)
    require_success(result, "check worktree status")
    return [line.strip() for line in result.stdout.splitlines() if line.strip() and status_entry_blocks_kickoff(line)]


def status_entry_blocks_kickoff(entry: str) -> bool:
    paths = status_entry_paths(entry)
    return any(not plugin_owned_status_path(path) for path in paths)


def plugin_owned_status_path(path: str) -> bool:
    return normalize_repo_path(path).startswith(".codex/linear-sync/")


def status_entry_paths(entry: str) -> list[str]:
    payload = entry[3:] if len(entry) > 3 else entry
    if " -> " in payload:
        return [decode_status_path(part) for part in payload.split(" -> ") if part.strip()]
    return [decode_status_path(payload)] if payload.strip() else []


def decode_status_path(path: str) -> str:
    value = path.strip()
    try:
        parts = shlex.split(value)
    except ValueError:
        return value.strip('"')
    if len(parts) == 1:
        return parts[0]
    return value.strip('"')


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
    events = load_events(root)
    if linear_guard_disabled(root=root):
        for path, event in events:
            event_id = str(event.get("id") or path.stem)
            if event_id not in set(local_state.get("processed_event_ids") or []):
                log_noop(local_state, event, "Linear sync disabled for this repo")
                mark_processed(local_state, event_id)
                skipped += 1
            safe_unlink(path)
        save_state(local_state, root)
        return {
            "processed": 0,
            "reviewed": 0,
            "skipped": skipped,
            "failed": 0,
        }
    active_problem = active_issue_fail_closed_problem(root=root)
    if active_problem and events:
        local_state.setdefault("failures", {})["active_state"] = {
            "count": len(events),
            "last_error": active_problem,
            "last_failed_at": now_iso(),
        }
        save_state(local_state, root)
        return {
            "processed": 0,
            "reviewed": 0,
            "skipped": 0,
            "failed": len(events),
            "active_state_error": active_problem,
        }

    for path, event in events:
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



def build_linear_comment(event: JsonDict, *, now: datetime | None = None) -> str:
    if event.get("type") == "session_progress":
        summary = str(event.get("summary") or "Codex made meaningful local progress.")
        current_state = str(event.get("diff_stat") or "Local files changed; inspect repo diff for details.")
        return with_codex_attribution(
            "\n".join(
                [
                    "Codex session progress update",
                    "",
                    "Summary:",
                    f"- {summary}",
                    "",
                    "Current state:",
                    f"- {current_state}",
                ]
            ),
            now=now,
        )

    short_sha = str(event.get("short_sha") or str(event.get("commit_sha") or "")[:7] or "unknown")
    subject = str(event.get("commit_subject") or "Commit progress")
    changed = [str(path) for path in event.get("changed_files") or [] if str(path).strip()]
    bullets = [str(item) for item in event.get("summary_bullets") or [] if str(item).strip()]
    if not bullets:
        bullets = summarize_changed_files(changed, subject=subject)
    changed_lines = changed_area_lines(changed)
    return with_codex_attribution(
        "\n".join(
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
        ),
        now=now,
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
    events = load_events(root)
    if linear_guard_disabled(root=root):
        for path, event in events[:limit]:
            event_id = str(event.get("id") or path.stem)
            skipped.append(
                {
                    "event_id": event_id,
                    "event_type": event.get("type"),
                    "event": event,
                    "inference": IssueInference(None, 0.0, "Linear sync disabled for this repo").__dict__,
                    "reason": "Linear sync disabled for this repo",
                }
            )
        return {
            "repo": str(repo_root(root)),
            "eligible": eligible,
            "held": held,
            "skipped": skipped,
            "instructions": [
                "Linear sync is disabled for this repo; do not write Linear progress updates.",
            ],
        }
    active_problem = active_issue_fail_closed_problem(root=root)
    if active_problem:
        for path, event in events:
            event_id = str(event.get("id") or path.stem)
            held.append(
                {
                    "event_id": event_id,
                    "event_type": event.get("type"),
                    "event": event,
                    "inference": IssueInference(None, 0.0, active_problem).__dict__,
                    "reason": active_problem,
                }
            )
            if len(held) >= limit:
                break
        return {
            "repo": str(repo_root(root)),
            "eligible": eligible,
            "held": held,
            "skipped": skipped,
            "instructions": [
                "Fix the active Linear issue state before writing Linear updates.",
                "Do not acknowledge queued events until active state is valid and the Linear comment is confirmed visible.",
            ],
        }

    for path, event in events:
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

def build_codex_prompt(event: JsonDict, inference: IssueInference, *, now: datetime | None = None) -> str:
    event_json = json.dumps(event, indent=2, sort_keys=True)
    comment_now = now or datetime.now(timezone.utc)
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
    comment_template = with_codex_attribution(comment_template, now=comment_now)
    footer = codex_attribution_footer(now=comment_now)

    return f"""You are Linear Progress Sync running inside Codex.

Use the existing Linear MCP/app connection only. Do not use a custom Linear API client.

Hard safety rules:
- Never mark any issue Done, Completed, Closed, Canceled, or any terminal state.
- Only add a concise comment to issue {inference.issue_key}.
- Optionally move issue {inference.issue_key} to In Progress only if the current Linear state is non-terminal.
- If issue {inference.issue_key} is already terminal, do not modify Linear.
- Do not create duplicate comments if this exact commit/update already appears on the issue.
- End the Linear comment exactly with: {footer}
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
    is_linear_write = linear_tool_is_write(normalized_tool)
    is_codex_file_edit = codex_file_edit_tool(payload, normalized_tool)
    user_profile_required = not linear_user_profile_configured()
    guard_enabled = linear_guard_enabled(root=root)
    guard_disabled = linear_guard_disabled(root=root)
    if guard_disabled and not is_linear_write:
        return PreToolGuardDecision(False)
    if normalized_tool == "bash":
        command = tool_command(payload)
        if is_linear_start_command(command):
            if user_profile_required and not linear_start_allowed_without_user_profile(command):
                return PreToolGuardDecision(True, linear_user_profile_required_message(root=root))
            return PreToolGuardDecision(False)
        return PreToolGuardDecision(False)

    if is_codex_file_edit:
        if user_profile_required:
            return PreToolGuardDecision(True, linear_user_profile_required_message(root=root))
        if not guard_enabled:
            return PreToolGuardDecision(True, linear_repo_binding_required_message(root=root))
        problem = active_issue_write_problem(root=root)
        if problem:
            return PreToolGuardDecision(True, linear_kickoff_required_message(problem, root=root))
        return PreToolGuardDecision(False)

    if is_linear_write:
        if user_profile_required:
            return PreToolGuardDecision(True, linear_user_profile_required_message(root=root))
        attribution_problem = linear_write_attribution_problem(payload, normalized_tool)
        if attribution_problem:
            return PreToolGuardDecision(True, attribution_problem)

    return PreToolGuardDecision(False)


def codex_file_edit_tool(payload: JsonDict, normalized_tool: str | None = None) -> bool:
    tool = normalized_tool if normalized_tool is not None else tool_name(payload).lower()
    # Codex file edits are reported as apply_patch. Edit/Write/MultiEdit are kept
    # as compatibility aliases for older hook matcher names and test fixtures.
    if tool in {
        "apply_patch",
        "edit",
        "multiedit",
        "write",
    }:
        return True
    return False


def linear_guard_enabled(*, root: str | Path | None = None) -> bool:
    if linear_guard_disabled(root=root):
        return False
    loaded = load_active_issue(root=root)
    if loaded.exists:
        return True
    return bool(repo_binding_status(root=root).get("configured"))


def linear_guard_disabled(*, root: str | Path | None = None) -> bool:
    return bool(repo_binding_status(root=root).get("disabled"))


def linear_start_script_path() -> str:
    return str(Path(__file__).with_name("linear_start.py"))


def linear_tool_is_write(normalized_tool: str) -> bool:
    return normalized_tool in {
        "mcp__codex_apps__linear._save_issue",
        "mcp__codex_apps__linear._save_comment",
        "mcp__linear.save_issue",
        "mcp__linear.save_comment",
        "save_issue",
        "save_comment",
    }


def linear_write_kind(normalized_tool: str) -> str | None:
    if normalized_tool in {"mcp__codex_apps__linear._save_comment", "mcp__linear.save_comment", "save_comment"}:
        return "comment"
    if normalized_tool in {"mcp__codex_apps__linear._save_issue", "mcp__linear.save_issue", "save_issue"}:
        return "issue"
    return None


def linear_write_text_keys(kind: str) -> set[str]:
    common = {"body", "content", "markdown", "text"}
    if kind == "comment":
        return common | {"comment", "comment_body", "message"}
    if kind == "issue":
        return common | {"description"}
    return set()


def linear_write_text_values(value: Any, keys: set[str]) -> list[str]:
    values: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in keys and isinstance(item, str) and item.strip():
                values.append(item)
            values.extend(linear_write_text_values(item, keys))
    elif isinstance(value, list):
        for item in value:
            values.extend(linear_write_text_values(item, keys))
    return values


def linear_write_payload_objects(payload: JsonDict) -> list[JsonDict]:
    objects: list[JsonDict] = []
    for key in ("tool_input", "toolInput", "input", "arguments"):
        value = payload.get(key)
        if isinstance(value, dict):
            objects.append(value)
    return objects or [payload]


def linear_write_field(payload: JsonDict, field: str) -> Any:
    for obj in linear_write_payload_objects(payload):
        if field in obj:
            return obj.get(field)
    return None


def linear_write_has_nonempty_field(payload: JsonDict, field: str) -> bool:
    value = linear_write_field(payload, field)
    return value is not None and str(value).strip() != ""


def linear_text_has_codex_footer(text: str, *, linear_name: str) -> bool:
    pattern = (
        r"(?:^|\n)Codex bot: "
        + re.escape(linear_name)
        + r" at \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00\s*$"
    )
    return re.search(pattern, text) is not None


def linear_issue_text_has_template(text: str) -> bool:
    expected_order = ("what", "why", "how")
    sections: dict[str, list[str]] = {}
    current_section: str | None = None
    next_section_index = 0
    for line in text.splitlines():
        stripped = line.strip()
        section = stripped.lower()
        if section in {"what", "why", "how"}:
            if next_section_index >= len(expected_order) or section != expected_order[next_section_index]:
                return False
            next_section_index += 1
            current_section = section
            sections.setdefault(section, [])
        elif current_section and stripped and not stripped.startswith("Codex bot:"):
            sections[current_section].append(stripped)
    return all("\n".join(sections.get(section, [])).strip() for section in ("what", "why", "how"))


def linear_write_attribution_problem(payload: JsonDict, normalized_tool: str) -> str | None:
    kind = linear_write_kind(normalized_tool)
    if not kind:
        return None
    status = linear_user_profile_status()
    profile = status.get("profile") if status.get("configured") else None
    linear_name = str(profile.get("linear_name") or "").strip() if isinstance(profile, dict) else ""
    if not linear_name:
        return None
    texts: list[str] = []
    for obj in linear_write_payload_objects(payload):
        texts.extend(linear_write_text_values(obj, linear_write_text_keys(kind)))
    if kind == "issue" and not linear_write_has_nonempty_field(payload, "id"):
        assignee = linear_write_field(payload, "assignee")
        assignee_ok = isinstance(assignee, str) and assignee.strip() == linear_name
        attribution_ok = bool(texts) and all(linear_text_has_codex_footer(text, linear_name=linear_name) for text in texts)
        template_ok = bool(texts) and all(linear_issue_text_has_template(text) for text in texts)
        if not assignee_ok or not attribution_ok or not template_ok:
            return linear_issue_create_required_message(
                linear_name=linear_name,
                needs_assignee=not assignee_ok,
                needs_attribution=not attribution_ok,
                needs_template=not template_ok,
            )
    if kind == "comment" and not texts:
        return linear_attribution_required_message(linear_name=linear_name)
    if any(not linear_text_has_codex_footer(text, linear_name=linear_name) for text in texts):
        return linear_attribution_required_message(linear_name=linear_name)
    return None


def linear_attribution_required_message(*, linear_name: str) -> str:
    return (
        "LINEAR ATTRIBUTION REQUIRED. Do not create or update Linear issue bodies or comments until the "
        "outgoing content ends with "
        f"`Codex bot: {linear_name} at <ISO-8601 UTC timestamp>`. Add the footer and retry the blocked "
        "Linear write."
    )


def linear_issue_create_required_message(
    *,
    linear_name: str,
    needs_assignee: bool,
    needs_attribution: bool,
    needs_template: bool,
) -> str:
    requirements: list[str] = []
    if needs_assignee:
        requirements.append(f"assign the new Linear issue to {linear_name}")
    if needs_template:
        requirements.append("use the What / Why / How template")
    if needs_attribution:
        requirements.append("include an attributed description ending with the Codex bot footer")
    joined = " and ".join(requirements)
    return (
        "LINEAR ISSUE CREATE REQUIRED. Do not create the Linear issue until the payload does this: "
        f"{joined}. Use `assignee: {linear_name}`, describe the issue with `What`, `Why`, and `How` "
        "sections, and end the description with "
        f"`Codex bot: {linear_name} at <ISO-8601 UTC timestamp>`."
    )


def linear_guard_repo_arg(*, root: str | Path | None = None) -> str:
    return str(repo_root(root))


def linear_user_profile_required_message(*, root: str | Path | None = None) -> str:
    repo_arg = shlex.quote(linear_guard_repo_arg(root=root))
    return (
        "LINEAR USER REQUIRED. Do not write code, create branches, create Linear issues, or continue kickoff "
        "until the global Linear user profile is saved. Your next action must be to list Linear users with "
        "mcp__codex_apps__linear._list_users or mcp__linear.list_users, present the active human users, "
        "ask the human to choose their Linear user from that list, then save the selected Linear `name` globally with "
        f"`python3 {linear_start_script_path()} configure-user --linear-name \"<Linear user name>\"`. "
        f"You can inspect the current value with `python3 {linear_start_script_path()} user-profile --root "
        f"{repo_arg}`. After this is saved, assign new Linear issues to that stored user, append "
        "`Codex bot: <stored Linear user name> at <ISO-8601 UTC timestamp>` to every Linear issue/comment "
        "body Codex creates, and retry the blocked tool call."
    )


def continue_kickoff_instruction(
    *,
    root: str | Path | None = None,
    needs_binding: bool = False,
    include_hard_stop: bool = True,
) -> str:
    if needs_binding:
        hard_stop = (
            "LINEAR DESTINATION REQUIRED. Do not answer with a code patch, do not say you are blocked, "
            "and do not stop. Your next action must be to list Linear teams/projects, present the Linear "
            "project list, then ask the human to choose the Linear project from that list. "
            if include_hard_stop
            else ""
        )
        return (
            f"{hard_stop}"
            "This is the one required first-run human "
            "question; do not ask for a Linear issue key and do not ask for coding approval. After the "
            "human chooses, save the answer before creating an issue, opening a PR, or applying code edits. Use the "
            "connected Linear app tools to list destinations: mcp__codex_apps__linear._list_teams and "
            "mcp__codex_apps__linear._list_projects, or mcp__linear.list_teams and "
            "mcp__linear.list_projects. If the session exposes short Linear aliases like list_teams or "
            "list_projects, use those aliases. If save_issue is not visible yet, search/load Linear tools "
            "and do not stop after listing projects. Present the Linear project list and ask the human to "
            "choose the Linear project from that list; do not ask them to type an unconstrained project name. "
            "Save the chosen destination with "
            f"`python3 {linear_start_script_path()} configure-repo --root "
            f"{shlex.quote(linear_guard_repo_arg(root=root))} --team \"<Linear team>\" "
            "--project \"<Linear project>\"`. Then create the Linear issue with "
            "mcp__codex_apps__linear._save_issue or mcp__linear.save_issue, using the saved team/project "
            "and assigning it to the saved Linear user name. End any Linear issue body or comment Codex "
            "creates with `Codex bot: <stored Linear user name> at <ISO-8601 UTC timestamp>`. "
            "After the issue exists, run "
            f"`python3 {linear_start_script_path()} kickoff --root "
            f"{shlex.quote(linear_guard_repo_arg(root=root))} --issue-key <ISSUE-KEY> "
            "--issue-title \"<Linear issue title>\" --issue-url <Linear issue URL> "
            "--branch <Linear gitBranchName>`; then attach/comment the draft PR in Linear with "
            "mcp__codex_apps__linear._save_issue/mcp__codex_apps__linear._save_comment or "
            "mcp__linear.save_issue/mcp__linear.save_comment, run the helper's `activation_command`, "
            "and retry the blocked tool call."
        )

    return (
        "This is not a request for human approval. Codex must continue the kickoff workflow itself. "
        "Do not test write access, do not inspect .codex as a substitute for kickoff, and do not stop after "
        "creating the Linear issue. "
        "If a Linear issue was already created in this turn, reuse that issue key, URL, title, and Linear "
        "gitBranchName. Otherwise create it in the saved team/project and assign it to the saved Linear "
        "user name. End any Linear issue body or comment Codex creates with `Codex bot: <stored Linear "
        "user name> at <ISO-8601 UTC timestamp>`. Run "
        f"`python3 {linear_start_script_path()} kickoff --root {shlex.quote(linear_guard_repo_arg(root=root))} "
        "--issue-key <ISSUE-KEY> --issue-title \"<Linear issue title>\" --issue-url <Linear issue URL> "
        "--branch <Linear gitBranchName>`; then attach/comment the draft PR in Linear, run the helper's "
        "`activation_command`, and retry the blocked tool call."
    )


def linear_repo_binding_required_message(*, root: str | Path | None = None) -> str:
    return (
        "LINEAR DESTINATION REQUIRED. Do not answer with a code patch, do not say you are blocked, "
        "and do not stop. Your next action must be to list Linear teams/projects, then ask the human "
        "which Linear team/project this repo should use. No Linear team/project is saved for this repo. "
        "Before applying Codex file edits, Codex must save that binding. "
        "Do not tell the user to configure it manually. "
        "Do not ask the user for a Linear issue key. "
        "Create a new Linear issue from the user's implementation request unless the user explicitly "
        "supplied an existing issue key. First call mcp__codex_apps__linear._list_teams and "
        "mcp__codex_apps__linear._list_projects, or mcp__linear.list_teams and mcp__linear.list_projects "
        "if the direct Linear namespace is exposed; present the Linear project list and ask the user only "
        "which Linear team/project this repo should use; save it with linear_start.py configure-repo before "
        "creating the issue or opening the PR. "
        f"{continue_kickoff_instruction(root=root, needs_binding=True, include_hard_stop=False)}"
    )


def linear_kickoff_required_message(reason: str | None = None, *, root: str | Path | None = None) -> str:
    prefix = f"{reason}. " if reason else ""
    return (
        f"{prefix}Linear kickoff is required before applying Codex file edits. "
        "Run the automatic Linear kickoff workflow first. Do not ask the user for a Linear issue key. "
        "Create a new Linear issue from the user's implementation request unless the user explicitly "
        "supplied an existing issue key; then create the Linear-named branch, push the empty kickoff "
        "commit, open the draft PR, link Linear and GitHub, activate active.json, and retry this tool call. "
        f"{continue_kickoff_instruction(root=root)}"
    )


def handle_post_tool_use(payload: JsonDict, *, root: str | Path | None = None) -> JsonDict | None:
    if linear_guard_disabled(root=root):
        return None
    tool = tool_name(payload)
    if tool.lower() == "bash":
        command = tool_command(payload)
        if command and tool_success(payload) and looks_like_git_commit(command):
            event = collect_commit_event(root=root)
            event["source"] = "PostToolUse:Bash"
            queued = enqueue_event("post_commit", event, root=root)
            spawn_drain(root=root)
            return queued
    if codex_file_edit_tool(payload, tool.lower()):
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


def shell_command_tokens(command: str) -> list[list[str]]:
    try:
        lexer = shlex.shlex(
            (command or "").replace("\n", " ; "),
            posix=True,
            punctuation_chars=SHELL_COMMAND_PUNCTUATION,
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        parsed = list(lexer)
    except ValueError:
        return []

    segments: list[list[str]] = []
    current: list[str] = []
    for token in parsed:
        if shell_token_is_separator(token):
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def shell_token_is_separator(token: str) -> bool:
    return bool(token) and all(char in SHELL_COMMAND_PUNCTUATION for char in token)


def shell_command_has_write_redirection(command: str) -> bool:
    in_single = False
    in_double = False
    escaped = False
    for char in command or "":
        if escaped:
            escaped = False
            continue
        if char == "\\" and not in_single:
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            continue
        if char == ">" and not in_single and not in_double:
            return True
    return False


def strip_env_prefix(tokens: list[str]) -> list[str]:
    index = 0
    if tokens and Path(tokens[0]).name == "env":
        index = 1
        env_value_options = {"-u", "--unset", "-C", "--chdir"}
        while index < len(tokens):
            token = tokens[index]
            if token in {"-i", "--ignore-environment", "-0", "--null"}:
                index += 1
                continue
            if token in {"-S", "--split-string"}:
                if index + 1 >= len(tokens):
                    return []
                return strip_env_prefix([*safe_shlex_split(tokens[index + 1]), *tokens[index + 2 :]])
            if token.startswith("--split-string="):
                return strip_env_prefix([*safe_shlex_split(token.split("=", 1)[1]), *tokens[index + 1 :]])
            if token in env_value_options:
                index += 2
                continue
            if any(token.startswith(f"{option}=") for option in env_value_options if option.startswith("--")):
                index += 1
                continue
            break
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            index += 1
            break
        if "=" in token and not token.startswith("-") and token.split("=", 1)[0]:
            index += 1
            continue
        break
    return tokens[index:]


def safe_shlex_split(value: str) -> list[str]:
    try:
        return shlex.split(value)
    except ValueError:
        return []


def is_linear_start_command(command: str) -> bool:
    return linear_start_command_subcommands(command) is not None


def linear_start_allowed_without_user_profile(command: str) -> bool:
    segments = linear_start_command_segments(command)
    if segments is None:
        return False
    return all(linear_start_segment_allowed_without_user_profile(tokens) for tokens in segments)


def linear_start_command_subcommands(command: str) -> list[str] | None:
    segments = linear_start_command_segments(command)
    if segments is None:
        return None
    return [str(linear_start_subcommand_from_tokens(tokens)) for tokens in segments]


def linear_start_command_segments(command: str) -> list[list[str]] | None:
    if shell_command_has_write_redirection(command):
        return None
    segments = shell_command_tokens(command or "")
    if not segments:
        return None
    subcommands = [linear_start_subcommand_from_tokens(tokens) for tokens in segments]
    if any(subcommand is None for subcommand in subcommands):
        return None
    return segments


def linear_start_segment_allowed_without_user_profile(tokens: list[str]) -> bool:
    subcommand = linear_start_subcommand_from_tokens(tokens)
    if subcommand in {"user-profile", "configure-user"}:
        return True
    if subcommand == "configure-repo":
        return "--disable-linear-sync" in tokens
    return False


def command_tokens_are_linear_start(tokens: list[str]) -> bool:
    return linear_start_subcommand_from_tokens(tokens) is not None


def linear_start_subcommand_from_tokens(tokens: list[str]) -> str | None:
    tokens = strip_env_prefix(tokens)
    if not tokens:
        return None
    executable = Path(tokens[0]).name
    script_index = 0
    if executable in {"python", "python3"}:
        if len(tokens) < 2:
            return None
        script_index = 1
    elif executable == "linear_start.py":
        script_index = 0
    elif tokens[0] == "/linear-start":
        return "/linear-start"
    else:
        return None
    script = Path(tokens[script_index]).name
    if script != "linear_start.py":
        return None
    if len(tokens) <= script_index + 1:
        return None
    subcommand = tokens[script_index + 1]
    return subcommand if subcommand in {
        "kickoff",
        "activate",
        "repo-binding",
        "configure-repo",
        "user-profile",
        "configure-user",
    } else None



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
    if tool_name(payload).lower() == "apply_patch":
        paths.extend(paths_from_apply_patch_command(tool_command(payload)))
    return sorted({normalize_repo_path(path) for path in paths if path})


def paths_from_apply_patch_command(command: str) -> list[str]:
    paths: list[str] = []
    for raw_line in command.splitlines():
        line = raw_line.strip()
        for prefix in (
            "*** Add File: ",
            "*** Update File: ",
            "*** Delete File: ",
            "*** Move to: ",
        ):
            if line.startswith(prefix):
                paths.append(line[len(prefix) :].strip())
                break
    return paths


def normalize_repo_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


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
