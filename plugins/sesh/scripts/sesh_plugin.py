"""Shared rules for the standalone `sesh` plugin's hooks.

The Sesh hooks (startup cue with the routine A/B, past-work cue, one-search-per-turn
guard, and Sesh search linking) used to ship only inside codex-session-logging. This
plugin carries the same code so Sesh keeps working where that logger was retired.

No hook may fire twice. Every codex-session-logging release since 0.2.18 already runs
these hooks itself, so this plugin steps aside whenever that logger is active and does
everything when it is not. codex-session-logging needs no change for this.
"""
from __future__ import annotations

import glob
import os
import re
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11.
    tomllib = None  # type: ignore[assignment]

PLUGIN_NAME = "sesh"
LOGGER_PLUGIN = "codex-session-logging"
# Hook event -> the codex-session-logging script that handles it (see its hooks/hooks.json).
LOGGER_SCRIPTS = {
    "SessionStart": "session_start.py",
    "UserPromptSubmit": "user_prompt_submit.py",
    "PreToolUse": "pre_tool_use.py",
    "PostToolUse": "post_tool_use.py",
}
# Testing and incident override: "sesh" always runs this plugin's hooks, "codex-session-logging"
# always steps aside. Anything else (the default) detects the logger.
OWNER_ENV = "E3_SESH_HOOK_OWNER"
# E3_SESH_LINKING=0/false/no/off stops this plugin posting its Sesh linkage events; cues and the
# guard are unaffected. The logger's own upload opt-out (CODEX_SESSION_LOG_AUTO_UPLOAD=0 or its
# saved preference) is honoured too.
LINKING_ENV = "E3_SESH_LINKING"
_PLUGIN_SECTION = re.compile(r'^\s*\[plugins\."([^"]+)"\]\s*$', re.MULTILINE)


def _falsy(value) -> bool:
    return value is not None and value.strip().lower() in {"0", "false", "no", "off"}


def codex_home(env=None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def _enabled_plugins(config_path: Path) -> dict[str, bool]:
    try:
        raw = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}
    plugins = None
    if tomllib is not None:
        try:
            plugins = tomllib.loads(raw).get("plugins")
        except Exception:  # noqa: BLE001 - fall back to the section headers.
            plugins = None
    if not isinstance(plugins, dict):
        # Same fallback the logger uses: a [plugins."id"] section means installed and enabled.
        return {name: True for name in _PLUGIN_SECTION.findall(raw)}
    return {
        str(name): not (isinstance(value, dict) and value.get("enabled") is False)
        for name, value in plugins.items()
    }


def _logger_enabled(env) -> bool:
    home = codex_home(env)
    for plugin_id, enabled in _enabled_plugins(home / "config.toml").items():
        if enabled and plugin_id.split("@", 1)[0] == LOGGER_PLUGIN:
            return True
    # Older installs merged the logger's hooks into the global hooks file instead.
    try:
        return f"/{LOGGER_PLUGIN}/" in (home / "hooks.json").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False


def _logger_script_installed(event: str, env) -> bool:
    script = LOGGER_SCRIPTS.get(event)
    if script is None:
        return False
    # The exact glob the logger's hook commands run (they use $HOME, not CODEX_HOME).
    home = Path(env.get("HOME") or Path.home())
    pattern = str(home / ".codex" / "plugins" / "cache" / "*" / LOGGER_PLUGIN / "*" / "scripts" / script)
    return any(os.path.isfile(path) for path in glob.glob(pattern))


def logger_runs(event: str, env=None) -> bool:
    """True when codex-session-logging will run its own Sesh hooks for this event.

    That logger runs them for every Sesh feature it has, so this plugin then does nothing
    for the event. Errors count as "logger absent": the cue then comes from this plugin,
    which is what a machine with the logger retired needs.
    """
    env = os.environ if env is None else env
    owner = (env.get(OWNER_ENV) or "").strip().lower()
    if owner == PLUGIN_NAME:
        return False
    if owner == LOGGER_PLUGIN:
        return True
    try:
        return _logger_enabled(env) and _logger_script_installed(event, env)
    except Exception:  # noqa: BLE001
        return False


def linking_enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return not _falsy(env.get(LINKING_ENV))


def is_sesh_tool(name) -> bool:
    """Every Sesh tool and the source-open tools its results point to; nothing else is logged."""
    if not isinstance(name, str):
        return False
    from session_logging import SOURCE_OPEN_TOOLS, is_sesh_search_tool

    return is_sesh_search_tool(name) or name in SOURCE_OPEN_TOOLS or "sesh__" in name

