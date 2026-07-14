#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from linear_sync import cli_root_arg, setup_plan


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up linear-progress-sync once for Codex.")
    cli_root_arg(parser)
    parser.add_argument(
        "--with-git-hook",
        action="store_true",
        help="Also install the optional repo post-commit hook for non-Codex commits.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the setup plan without running commands.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable setup output.")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[3]
    plan = setup_plan(plugin_repo_root=repo_root, target_repo_root=args.root, with_git_hook=args.with_git_hook)
    if args.dry_run:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return

    result = run_setup_plan(plan)
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print_summary(result)
    if not result["ok"]:
        raise SystemExit(1)


def run_setup_plan(plan: dict) -> dict:
    results: list[dict] = []
    for command in plan["commands"]:
        results.append(run_step(command))
        if not results[-1]["ok"]:
            break
    return {"ok": all(item["ok"] for item in results), "results": results, "plan": plan}


def run_step(command: str) -> dict:
    argv = shlex.split(command)
    if not argv:
        return {"command": command, "ok": True, "message": "empty command skipped"}
    executable = argv[0]
    if shutil.which(executable) is None:
        return {
            "command": command,
            "ok": False,
            "message": missing_executable_message(executable),
        }
    print(f"Running: {command}", file=sys.stderr)
    completed = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    output = "\n".join(part for part in (completed.stdout.strip(), completed.stderr.strip()) if part)
    if completed.returncode == 0 or is_idempotent_setup_success(argv, output):
        return {"command": command, "ok": True, "message": output}
    if argv[:3] == ["gh", "auth", "status"]:
        output = f"{output}\nRun: gh auth login".strip()
    return {"command": command, "ok": False, "message": output or str(completed.returncode)}


def is_idempotent_setup_success(argv: list[str], output: str) -> bool:
    idempotent_prefixes = (
        ["codex", "plugin", "marketplace", "add"],
        ["codex", "plugin", "add"],
        ["codex", "mcp", "add"],
    )
    if not any(argv[: len(prefix)] == prefix for prefix in idempotent_prefixes):
        return False
    normalized_output = output.lower()
    idempotent_phrases = (
        "already exists",
        "already installed",
        "already added",
        "already configured",
        "exists already",
        "is already",
    )
    return any(phrase in normalized_output for phrase in idempotent_phrases)


def missing_executable_message(executable: str) -> str:
    if executable == "gh":
        return "GitHub CLI is required. Install it, then run: gh auth login"
    if executable == "codex":
        return "Codex CLI is required. Install/sign in to Codex, then rerun this setup script."
    return f"Required executable not found: {executable}"


def print_summary(result: dict) -> None:
    for item in result["results"]:
        status = "ok" if item["ok"] else "failed"
        print(f"[{status}] {item['command']}")
        if item.get("message"):
            print(item["message"])
    if result["ok"]:
        print("Setup complete.")
        print("")
        print("Next steps:")
        print("1. Run: codex mcp login linear")
        print("2. If Codex asks to review hooks, trust the Linear Progress Sync and Codex Session Logging hooks once.")
        print("3. Start a new Codex thread so hooks and skills reload.")
        print("4. On first use, Codex will list Linear users/projects and ask what to save.")
        print("")
        print("Notes:")
        print("- GitHub auth is checked by setup; run gh auth login if that step failed.")
        print("- Linear auth is manual; setup registers the MCP server but does not log you in.")
        print("- Repos can opt out with linear_start.py configure-repo --disable-linear-sync.")
        print("- Codex Session Logging is installed during setup and maintained by the resident updater.")
        print("- The resident updater checks at login and every 30 minutes; normal updates need no renewal thread.")
        print("- SessionStart remains a self-healing fallback; use update_plugin.py --disable-auto-update for a persistent opt-out.")
        print("- LINEAR_SYNC_AUTO_UPDATE=0 is also persisted when setup or a self-healing hook observes it.")
        print("- Before Linear kickoff, file edits, write-like Bash commands, and branch creation wait for active Linear state.")
        print("- No per-repo setup is needed unless you want the optional outside-Codex Git commit hook.")


if __name__ == "__main__":
    main()
