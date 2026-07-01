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
DRY_RUN_ENV = "LINEAR_SYNC_DRY_RUN"
CODEX_COMMAND_ENV = "LINEAR_SYNC_CODEX_COMMAND"
THROTTLE_SECONDS = 30 * 60
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
    files_raw = git_output(["diff-tree", "--no-commit-id", "--name-only", "-r", commit_sha], root=root)
    return {
        "commit_sha": commit_sha,
        "short_sha": commit_sha[:7],
        "commit_subject": subject,
        "commit_body": body,
        "changed_files": [line for line in files_raw.splitlines() if line.strip()],
        "branch": current_branch(root),
    }


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
