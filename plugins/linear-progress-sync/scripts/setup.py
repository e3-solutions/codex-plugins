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
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - setup requires Python 3.11 in production.
    tomllib = None  # type: ignore[assignment]

from linear_sync import cli_root_arg, setup_plan

E3_COSMOS_URL = "https://cosmos.e3g.ai/e3/mcp"
SESH_SEARCH_TOOL = "sesh__search_coding_sessions"
SESH_SOURCE_OPEN_TOOL = "timetracker__get_chat"
# Keep this aligned with codex-session-logging/hooks/hooks.json. These five
# events cover live capture plus parent/subagent coordination discovery.
REQUIRED_SESSION_LOGGING_HOOK_EVENTS = frozenset(
    {"SessionStart", "PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop"}
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up linear-progress-sync once for Codex.")
    cli_root_arg(parser)
    parser.add_argument(
        "--with-git-hook",
        action="store_true",
        help="Also install the optional repo post-commit hook for non-Codex commits.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the setup plan without running commands.")
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Inspect teammate session logging and Cosmos readiness without network or login actions.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable setup output.")
    args = parser.parse_args()

    if args.doctor:
        if args.dry_run or args.with_git_hook:
            parser.error("--doctor cannot be combined with setup options")
        result = teammate_readiness(target_repo_root=args.root)
        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            print_doctor_summary(result)
        if not result["locally_ready"]:
            raise SystemExit(1)
        return

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
    completed = subprocess.run(argv, text=True, capture_output=True, check=False)
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


def teammate_readiness(
    *,
    target_repo_root: str | Path | None = None,
    codex_home_path: str | Path | None = None,
) -> dict:
    """Inspect local prerequisites without contacting GitHub, Cosmos, or Supabase."""

    codex_home = Path(
        codex_home_path or os.environ.get("CODEX_HOME") or Path.home() / ".codex"
    ).expanduser().resolve()
    root = Path(target_repo_root or os.getcwd()).expanduser().resolve()
    plugin = _session_logging_install(codex_home)
    hooks = _session_logging_hooks(codex_home)
    upload = _session_logging_upload(codex_home)
    repository = _repository_readiness(root)
    cosmos = _cosmos_readiness(codex_home)

    issues: list[str] = []
    if not plugin["installed"]:
        issues.append("Codex Session Logging plugin is not installed.")
    elif not plugin["enabled"]:
        issues.append("Codex Session Logging plugin is installed but not enabled.")
    if not hooks["installed"]:
        issues.append("The installed Codex Session Logging plugin has no readable native hooks.")
    if not upload["enabled"]:
        issues.append("Session upload is disabled; remove the opt-out before expecting new sessions to sync.")
    if upload["dead_letter"]:
        issues.append("Session logging has dead-letter records that need local inspection.")
    if repository["eligible"] is not True:
        issues.append("The target repository origin is not a verified e3-solutions GitHub remote.")
    if not cosmos["configured"]:
        issues.append("E3 Cosmos is not configured at the expected endpoint.")

    locally_ready = not issues
    next_steps = list(issues)
    if locally_ready:
        next_steps.append(
            "In Codex, verify E3 Cosmos identity and confirm the Sesh search tool is exposed; "
            "then run one ordinary search and open its returned source handle."
        )
    return {
        "schema_version": 1,
        "locally_ready": locally_ready,
        "live_verification_required": True,
        "checks": {
            "session_logging_plugin": plugin,
            "session_logging_hooks": hooks,
            "session_upload": upload,
            "repository": repository,
            "e3_cosmos": cosmos,
        },
        "issues": issues,
        "next_steps": next_steps,
    }


def _session_logging_install(codex_home: Path) -> dict:
    manifests = sorted(
        codex_home.glob("plugins/cache/*/codex-session-logging/*/.codex-plugin/plugin.json")
    )
    valid = []
    for path in manifests:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and value.get("name") == "codex-session-logging":
            valid.append(value)
    versions = sorted(
        {str(item["version"]) for item in valid if isinstance(item.get("version"), str)}
    )
    config, _ = _read_codex_config(codex_home)
    plugins = config.get("plugins") if isinstance(config, dict) else None
    configured = plugins.get("codex-session-logging@coreedge-local") if isinstance(plugins, dict) else None
    enabled = isinstance(configured, dict) and configured.get("enabled") is True
    return {"installed": bool(valid), "enabled": enabled, "versions": versions}


def _session_logging_hooks(codex_home: Path) -> dict:
    valid_events = set()
    declared_events = set()
    manifests = codex_home.glob(
        "plugins/cache/*/codex-session-logging/*/.codex-plugin/plugin.json"
    )
    for manifest_path in manifests:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict) or manifest.get("name") != "codex-session-logging":
                continue
            plugin_root = manifest_path.parents[1].resolve()
            relative = manifest.get("hooks")
            if not isinstance(relative, str) or not relative:
                continue
            hook_path = (plugin_root / relative).resolve()
            hook_path.relative_to(plugin_root)
            payload = json.loads(hook_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            continue
        hooks = payload.get("hooks") if isinstance(payload, dict) else None
        if isinstance(hooks, dict):
            declared_events.update(str(event) for event in hooks)
            valid_events.update(
                str(event)
                for event, value in hooks.items()
                if _hook_event_has_command(value)
            )
    missing = sorted(REQUIRED_SESSION_LOGGING_HOOK_EVENTS - declared_events)
    malformed = sorted(
        REQUIRED_SESSION_LOGGING_HOOK_EVENTS & declared_events - valid_events
    )
    ordered_events = sorted(valid_events & REQUIRED_SESSION_LOGGING_HOOK_EVENTS)
    return {
        "installed": not missing and not malformed,
        "events": ordered_events,
        "missing_events": missing,
        "malformed_events": malformed,
    }


def _hook_event_has_command(value: object) -> bool:
    if not isinstance(value, list):
        return False
    for group in value:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            continue
        for hook in group["hooks"]:
            if (
                isinstance(hook, dict)
                and hook.get("type") == "command"
                and isinstance(hook.get("command"), str)
                and bool(hook["command"].strip())
            ):
                return True
    return False


def _session_logging_upload(codex_home: Path) -> dict:
    state = Path(os.environ.get("CODEX_SESSION_LOG_STATE_DIR") or codex_home / "session-logging")
    explicit = os.environ.get("CODEX_SESSION_LOG_AUTO_UPLOAD")
    source = "default"
    if explicit is not None:
        enabled = explicit.strip().lower() not in {"0", "false", "no", "off"}
        source = "environment"
    else:
        try:
            preference = json.loads((codex_home / "session-logging/preferences.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, ValueError):
            preference = {}
        enabled = preference.get("enabled", True) if isinstance(preference, dict) else True
        enabled = enabled if isinstance(enabled, bool) else True
        if isinstance(preference, dict) and isinstance(preference.get("enabled"), bool):
            source = "preference"
    pending = _count_json(state / "queue/pending") + _count_json(state / "queue")
    return {
        "enabled": enabled,
        "preference_source": source,
        "queue_observed": state.exists(),
        "pending": pending,
        "processing": _count_json(state / "queue/processing"),
        "dead_letter": _count_json(state / "queue/dead-letter"),
    }


def _count_json(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(1 for item in path.glob("*.json") if item.is_file())


def _repository_readiness(root: Path) -> dict:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except OSError:
        completed = None
    remote = completed.stdout.strip() if completed is not None and completed.returncode == 0 else ""
    repository = _e3_repository_name(remote)
    return {
        "eligible": True if repository else (False if remote else None),
        "organization": "e3-solutions" if repository else None,
        "repository": repository,
    }


def _e3_repository_name(remote: str) -> str | None:
    value = remote.strip()
    https = re.fullmatch(
        r"https://github\.com/e3-solutions/(?P<repository>[^/]+?)(?:\.git)?/?",
        value,
        re.IGNORECASE,
    )
    if https:
        return https.group("repository")
    scp = re.fullmatch(
        r"git@(?P<host>[^:/\s]+):e3-solutions/(?P<repository>[^/]+?)(?:\.git)?/?",
        value,
        re.IGNORECASE,
    )
    ssh = re.fullmatch(
        r"ssh://git@(?P<host>[^/\s:]+)(?::\d+)?/e3-solutions/"
        r"(?P<repository>[^/]+?)(?:\.git)?/?",
        value,
        re.IGNORECASE,
    )
    match = scp or ssh
    if not match or not _ssh_host_resolves_to_github(match.group("host")):
        return None
    return match.group("repository")


def _ssh_host_resolves_to_github(host: str) -> bool:
    if host.rstrip(".").lower() == "github.com":
        return True
    try:
        result = subprocess.run(
            ["ssh", "-G", host],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0:
        return False
    for line in result.stdout.splitlines():
        key, separator, value = line.partition(" ")
        if key.lower() == "hostname" and separator:
            return value.strip().rstrip(".").lower() == "github.com"
    return False


def _cosmos_readiness(codex_home: Path) -> dict:
    payload, parse_error = _read_codex_config(codex_home)
    servers = payload.get("mcp_servers") if isinstance(payload, dict) else None
    server = servers.get("e3-cosmos") if isinstance(servers, dict) else None
    endpoint = server.get("url") if isinstance(server, dict) else None
    endpoint_matches = isinstance(endpoint, str) and endpoint.rstrip("/") == E3_COSMOS_URL
    enabled = isinstance(server, dict) and server.get("enabled", True) is not False
    tools = server.get("tools", {}) if isinstance(server, dict) else {}
    declared = sorted(
        name for name in (SESH_SEARCH_TOOL, SESH_SOURCE_OPEN_TOOL)
        if isinstance(tools, dict) and name in tools
    )
    return {
        "configured": bool(endpoint_matches and enabled and not parse_error),
        "endpoint_matches": bool(endpoint_matches),
        "enabled": bool(enabled),
        "config_parse_error": parse_error,
        "expected_capabilities": {
            "search": SESH_SEARCH_TOOL,
            "source_open": SESH_SOURCE_OPEN_TOOL,
        },
        "declared_tool_overrides": declared,
        "authentication": "unverified_non_interactively",
        "live_tool_exposure": "unverified_non_interactively",
    }


def _read_codex_config(codex_home: Path) -> tuple[dict, bool]:
    if tomllib is None:
        return {}, True
    try:
        payload = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, False
    except (OSError, ValueError):
        return {}, True
    return (payload, False) if isinstance(payload, dict) else ({}, True)


def print_doctor_summary(result: dict) -> None:
    checks = result["checks"]
    print("Sesh teammate readiness")
    plugin = checks["session_logging_plugin"]
    print(
        f"- Session Logging plugin: {_yes_no(plugin['installed'])} "
        f"({'enabled' if plugin['enabled'] else 'disabled'})"
    )
    print(f"- Session Logging hooks: {_yes_no(checks['session_logging_hooks']['installed'])}")
    upload = checks["session_upload"]
    print(
        f"- Upload: {'enabled' if upload['enabled'] else 'disabled'} "
        f"(pending {upload['pending']}, processing {upload['processing']}, "
        f"dead-letter {upload['dead_letter']})"
    )
    repository = checks["repository"]
    repo_label = repository["repository"] or "not verified"
    print(f"- E3 repository: {_yes_no(repository['eligible'] is True)} ({repo_label})")
    cosmos = checks["e3_cosmos"]
    print(f"- E3 Cosmos endpoint: {_yes_no(cosmos['configured'])}")
    print(
        f"- Expected capabilities: {cosmos['expected_capabilities']['search']} and "
        f"{cosmos['expected_capabilities']['source_open']}"
    )
    print("- Cosmos authentication/tool exposure: requires live verification in Codex")
    print("- Run this doctor from the marketplace clone or the installed plugin cache; it makes no changes.")
    print(f"Local readiness: {'ready' if result['locally_ready'] else 'needs action'}")
    if result["next_steps"]:
        print("Next:")
        for step in result["next_steps"]:
            print(f"- {step}")


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def print_summary(result: dict) -> None:
    for item in result["results"]:
        status = "ok" if item["ok"] else "failed"
        print(f"[{status}] {item['command']}")
        if item.get("message"):
            print(item["message"])
    if result["ok"]:
        print("Setup complete.")
        print()
        print("Next steps:")
        print("1. Run: codex mcp login linear")
        print("2. Run: codex mcp login e3-cosmos")
        print("3. If Codex asks to review hooks, trust the Linear Progress Sync and Codex Session Logging hooks once.")
        print("4. Start a new Codex thread so hooks and skills reload.")
        print("5. Verify Sesh search returns evidence, then open one returned Coding Sessions source.")
        print("6. On first use, Codex will list Linear users/projects and ask what to save.")
        print()
        print("Notes:")
        print("- GitHub auth is checked by setup; run gh auth login if that step failed.")
        print("- Linear auth is manual; setup registers the MCP server but does not log you in.")
        print("- E3 Cosmos auth is manual; setup registers the MCP server but does not log you in.")
        print("- Run setup.py --doctor from this clone or the installed plugin cache for local readiness checks.")
        print("- Repos can opt out with linear_start.py configure-repo --disable-linear-sync.")
        print("- Codex Session Logging is installed during setup and maintained by the resident updater.")
        print("- The resident updater checks at login and every 30 minutes through macOS LaunchAgents or Linux systemd user timers.")
        print("- On a headless Linux VM, enable loginctl lingering for this account if timers must continue after logout.")
        print("- SessionStart remains a self-healing fallback; use update_plugin.py --disable-auto-update for a persistent opt-out.")
        print("- LINEAR_SYNC_AUTO_UPDATE=0 is also persisted when setup or a self-healing hook observes it.")
        print("- Before Linear kickoff, file edits, write-like Bash commands, and branch creation wait for active Linear state.")
        print("- No per-repo setup is needed unless you want the optional outside-Codex Git commit hook.")


if __name__ == "__main__":
    main()
