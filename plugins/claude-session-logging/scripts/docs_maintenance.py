#!/usr/bin/env python3
"""Keep repo docs current at the moment code changes (COR-5202).

Called at the end of the Stop hook. When this machine has the feature on, the
working directory is an e3-solutions repository with agent docs, and the
branch or working tree has real code changes that do not touch the docs, it
returns a Stop "block" decision once per change set. Both Codex and Claude Code
then continue the turn with ``reason`` as the next prompt: the agent checks
AGENTS.md, docs/ and .agents/skills and updates them in the same change.

Off by default. Turn it on with ``E3_DOCS_MAINTENANCE=1`` or by creating
``~/.e3/docs-maintenance/enabled``; ``E3_DOCS_MAINTENANCE=0`` always wins.
Every failure is silent: the hook never stops the agent from finishing.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

MARKER = "[e3-docs-check v1]"
STATE_DIR = Path(os.environ.get("E3_DOCS_MAINTENANCE_HOME") or Path.home() / ".e3" / "docs-maintenance")
GIT_TIMEOUT_SECONDS = 2.0
MIN_CHANGED_LINES = 10
MAX_LISTED_FILES = 10
DOC_PREFIXES = ("docs/", ".agents/", ".claude/", ".codex/", ".github/ISSUE_TEMPLATE/")
DOC_NAMES = ("AGENTS.md", "CLAUDE.md")
NOT_CODE_SUFFIXES = (".lock", "-lock.json", "-lock.yaml", ".lockb", ".png", ".jpg", ".svg", ".pdf")


def enabled() -> bool:
    value = os.environ.get("E3_DOCS_MAINTENANCE", "").strip().lower()
    if value in ("0", "off", "false", "no"):
        return False
    if value in ("1", "on", "true", "yes"):
        return True
    return (STATE_DIR / "enabled").exists()


def git(cwd: str, *args: str) -> str | None:
    try:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                                timeout=GIT_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def is_doc(path: str) -> bool:
    """Agent docs and any Markdown (a README edit counts as updating docs)."""
    return path.endswith(".md") or path.startswith(DOC_PREFIXES)


def is_code(path: str) -> bool:
    return not is_doc(path) and not path.endswith(NOT_CODE_SUFFIXES)


def base_commit(root: str) -> str | None:
    """Where this branch left the default branch, or HEAD when on it."""
    head = (git(root, "rev-parse", "HEAD") or "").strip()
    for ref in ("origin/HEAD", "origin/main", "origin/master"):
        base = (git(root, "merge-base", "HEAD", ref) or "").strip()
        if base:
            return base
    return head or None


def change_set(root: str) -> dict[str, Any] | None:
    base = base_commit(root)
    if not base:
        return None
    numstat = git(root, "diff", "--numstat", "-M", base)
    status = git(root, "diff", "--name-status", "-M", base)
    untracked = git(root, "ls-files", "--others", "--exclude-standard")
    if numstat is None or status is None or untracked is None:
        return None
    lines = 0
    files: set[str] = set()
    structural = False
    for row in numstat.splitlines():
        added, deleted, path = (row.split("\t", 2) + ["", ""])[:3]
        path = path.split(" => ")[-1].strip("{}")
        files.add(path)
        if is_code(path):
            lines += (int(added) if added.isdigit() else 0) + (int(deleted) if deleted.isdigit() else 0)
    for row in status.splitlines():
        kind = row[:1]
        path = row.split("\t")[-1]
        if kind in ("A", "D", "R") and is_code(path):
            structural = True
    new_files = [p for p in untracked.splitlines() if p]
    files.update(new_files)
    if any(is_code(p) for p in new_files):
        structural = True
    return {
        "base": base,
        "files": sorted(files),
        "code": sorted(p for p in files if is_code(p)),
        "docs": sorted(p for p in files if is_doc(p)),
        "code_lines": lines,
        "structural": structural,
    }


def e3_repo_root(cwd: str) -> str | None:
    root = (git(cwd, "rev-parse", "--show-toplevel") or "").strip()
    if not root:
        return None
    origin = (git(root, "remote", "get-url", "origin") or "").strip().lower()
    if "github.com/e3-solutions/" not in origin and "github.com:e3-solutions/" not in origin:
        return None
    if not any((Path(root) / name).is_file() for name in DOC_NAMES):
        return None
    return root


def already_prompted(fingerprint: str) -> bool:
    return (STATE_DIR / "prompted" / fingerprint).exists()


def record(fingerprint: str, event: dict[str, Any]) -> None:
    try:
        (STATE_DIR / "prompted").mkdir(parents=True, exist_ok=True)
        (STATE_DIR / "prompted" / fingerprint).write_text(str(int(time.time())))
        with open(STATE_DIR / "events.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
    except OSError:
        pass


def reason_text(repo: str, changes: dict[str, Any]) -> str:
    listed = changes["code"][:MAX_LISTED_FILES]
    more = len(changes["code"]) - len(listed)
    files = ", ".join(f"`{p}`" for p in listed) + (f" and {more} more" if more > 0 else "")
    return (
        f"{MARKER} Before you finish: this change edits code in {repo} ({files}) but no agent docs. "
        "Check AGENTS.md, docs/README.md and any skill in .agents/skills/ that covers this area. "
        "If a command, deploy step, env var, branch, file path, service name or rule they state is now "
        "wrong or missing because of this change, fix it now in the same change (same commit or PR); "
        "keep it short and in the doc that already owns that fact. "
        "If .agents/check_agent_docs.py exists, run `python3 .agents/check_agent_docs.py`. "
        "If you commit, add the trailer `Docs-Check: updated` or `Docs-Check: none-needed`. "
        "If nothing needs changing, reply with one line, `Docs checked: no change needed`, and stop."
    )


def decide(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return the Stop hook output, or None to let the agent stop."""
    if not enabled() or payload.get("stop_hook_active"):
        return None
    cwd = str(payload.get("cwd") or os.getcwd())
    root = e3_repo_root(cwd)
    if not root:
        return None
    changes = change_set(root)
    if not changes or not changes["code"] or changes["docs"]:
        return None
    if changes["code_lines"] < MIN_CHANGED_LINES and not changes["structural"]:
        return None
    digest = hashlib.sha256(json.dumps([root, changes["base"], changes["files"], changes["code_lines"]]).encode())
    fingerprint = digest.hexdigest()[:24]
    if already_prompted(fingerprint):
        return None
    repo = Path(root).name
    record(fingerprint, {
        "at": int(time.time()), "repo": repo, "session_id": payload.get("session_id"),
        "code_files": len(changes["code"]), "code_lines": changes["code_lines"], "action": "prompted",
    })
    return {"decision": "block", "reason": reason_text(repo, changes)}


def run(payload: dict[str, Any]) -> None:
    """Stop-hook entry point: print the decision JSON, or nothing."""
    try:
        output = decide(payload)
    except Exception:  # noqa: BLE001 - never block a stop because of this check.
        return
    if output:
        print(json.dumps(output), flush=True)
