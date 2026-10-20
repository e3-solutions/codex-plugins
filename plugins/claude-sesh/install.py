"""Install the Claude Code `sesh` plugin (COR-4888) with one command. Safe to rerun.

    python3 plugins/claude-sesh/install.py              # install, repair or update
    python3 plugins/claude-sesh/install.py --uninstall  # remove it again

It needs no clone of its own: it only runs Claude Code's own plugin commands.

1. Registers the E3 Claude Code marketplace `coreedge-internal` (this repository) if it is not
   there yet, or refreshes it if it is. People who already use claude-session-logging have it.
2. `claude plugin install sesh@coreedge-internal --scope user` (or enables it if it was disabled,
   or updates it if an older version is installed).
3. Checks that every other plugin and marketplace is exactly as before.

It touches no other plugin, writes no hooks or settings by hand, and keeps the Sesh state in
`<Claude config dir>/sesh` (or the plugin data dir). It honours CLAUDE_CONFIG_DIR, so it can be
tried in a throwaway config first. If a `sesh` plugin from another marketplace is installed, it
does nothing, because two copies would run every hook twice. --uninstall removes only
`sesh@coreedge-internal` and leaves the marketplace, which claude-session-logging shares.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PLUGIN = "sesh"
MARKETPLACE = "coreedge-internal"
PLUGIN_ID = f"{PLUGIN}@{MARKETPLACE}"
SOURCES = (
    "https://github.com/e3-solutions/codex-plugins.git",
    "git@github.com:e3-solutions/codex-plugins.git",
)
CLAUDE_CANDIDATES = ("~/.local/bin/claude", "~/.claude/local/claude", "/opt/homebrew/bin/claude",
                     "/usr/local/bin/claude")


class InstallError(RuntimeError):
    pass


def find_claude(explicit: str | None = None) -> str:
    for candidate in ([explicit] if explicit else []) + [shutil.which("claude") or ""] + list(CLAUDE_CANDIDATES):
        if candidate:
            path = Path(candidate).expanduser()
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
    raise InstallError("Claude Code CLI not found; install Claude Code first (https://claude.com/claude-code)")


def run(claude: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run([claude, *args], capture_output=True, text=True, timeout=300)
    if check and result.returncode != 0:
        message = (result.stderr or result.stdout).strip().splitlines()
        raise InstallError(f"`claude {' '.join(args)}` failed: {message[-1] if message else result.returncode}")
    return result


def listing(claude: str, *args: str) -> list:
    loaded = json.loads(run(claude, *args, "--json").stdout or "[]")
    return loaded if isinstance(loaded, list) else []


def inventory(claude: str):
    plugins = {item.get("id"): item for item in listing(claude, "plugin", "list") if isinstance(item, dict)}
    markets = {item.get("name"): item for item in listing(claude, "plugin", "marketplace", "list")
               if isinstance(item, dict)}
    return plugins, markets


def _others(plugins: dict, markets: dict):
    keep = ("id", "version", "scope", "enabled")
    return ({key: {k: v.get(k) for k in keep} for key, v in plugins.items() if key != PLUGIN_ID},
            {key: {k: v.get(k) for k in ("name", "source", "url", "repo", "path")}
             for key, v in markets.items()})


def manifest_version(markets: dict) -> str | None:
    location = (markets.get(MARKETPLACE) or {}).get("installLocation")
    if not location:
        return None
    try:
        return json.loads((Path(location) / "plugins/claude-sesh/.claude-plugin/plugin.json")
                          .read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        return None


def install(claude: str, source: str | None = None) -> dict:
    plugins, markets = inventory(claude)
    conflicting = sorted(key for key in plugins if key and key.split("@", 1)[0] == PLUGIN and key != PLUGIN_ID)
    if conflicting:
        return {"status": "skipped", "reason": f"{', '.join(conflicting)} is already installed",
                "plugin": PLUGIN_ID}
    actions = []
    if MARKETPLACE in markets:
        run(claude, "plugin", "marketplace", "update", MARKETPLACE)
        actions.append("marketplace updated")
    else:
        errors = []
        for candidate in ([source] if source else list(SOURCES)):
            result = run(claude, "plugin", "marketplace", "add", candidate, check=False)
            if result.returncode == 0:
                actions.append(f"marketplace added from {candidate}")
                break
            errors.append((result.stderr or result.stdout).strip().splitlines()[-1:] or [""])
        else:
            raise InstallError("could not add the coreedge-internal marketplace (Git access to "
                               f"e3-solutions/codex-plugins is needed): {errors}")
    before = _others(*inventory(claude))
    plugins, markets = inventory(claude)
    if MARKETPLACE not in markets:
        raise InstallError(f"the marketplace was added but is not named {MARKETPLACE}")
    current = plugins.get(PLUGIN_ID)
    wanted = manifest_version(markets)
    if current is None:
        run(claude, "plugin", "install", PLUGIN_ID, "--scope", "user")
        actions.append("installed")
    else:
        if wanted and current.get("version") != wanted:
            run(claude, "plugin", "update", PLUGIN_ID)
            actions.append(f"updated {current.get('version')} -> {wanted}")
        if current.get("enabled") is False:
            run(claude, "plugin", "enable", PLUGIN_ID)
            actions.append("enabled")
    plugins, markets = inventory(claude)
    item = plugins.get(PLUGIN_ID)
    if not item or item.get("enabled") is False:
        raise InstallError(f"{PLUGIN_ID} is not installed and enabled after install")
    if _others(plugins, markets) != before:
        raise InstallError("another plugin or marketplace changed during install; check `claude plugin list`")
    return {"status": "installed" if "installed" in actions or any(a.startswith("updated") for a in actions)
            else "unchanged", "plugin": PLUGIN_ID, "version": item.get("version"),
            "install_path": item.get("installPath"), "actions": actions,
            "next": "Start a new Claude Code session in an e3-solutions repository."}


def uninstall(claude: str) -> dict:
    plugins, _ = inventory(claude)
    if PLUGIN_ID not in plugins:
        return {"status": "not-installed", "plugin": PLUGIN_ID}
    run(claude, "plugin", "uninstall", PLUGIN_ID)
    plugins, _ = inventory(claude)
    if PLUGIN_ID in plugins:
        raise InstallError(f"{PLUGIN_ID} is still installed")
    return {"status": "uninstalled", "plugin": PLUGIN_ID}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--uninstall", action="store_true", help="remove sesh@coreedge-internal")
    parser.add_argument("--claude", help="path to the claude binary")
    parser.add_argument("--source", help="marketplace source to add (default: the e3-solutions GitHub repo)")
    args = parser.parse_args(argv)
    try:
        claude = find_claude(args.claude)
        result = uninstall(claude) if args.uninstall else install(claude, args.source)
    except (InstallError, subprocess.TimeoutExpired, ValueError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result["status"] != "skipped" else 2


if __name__ == "__main__":
    sys.exit(main())
