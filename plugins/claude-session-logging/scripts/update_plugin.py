#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

JsonDict = dict[str, Any]

STATE_DIR_ENV = "CLAUDE_SESSION_LOG_STATE_DIR"
AUTO_UPDATE_ENV = "CLAUDE_SESSION_LOG_AUTO_UPDATE"
AUTO_UPDATE_INTERVAL_ENV = "CLAUDE_SESSION_LOG_AUTO_UPDATE_INTERVAL_SECONDS"
CLAUDE_CLI_ENV = "CLAUDE_SESSION_LOG_CLAUDE_CLI"
DEFAULT_AUTO_UPDATE_INTERVAL_SECONDS = 24 * 60 * 60
MARKETPLACE_NAME = "coreedge-internal"
PLUGIN_ID = "claude-session-logging@coreedge-internal"
UPDATE_TIMEOUT_SECONDS = 120


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Refresh the Core Edge Claude Code marketplace and plugin.")
    parser.add_argument("--state-path", type=Path, default=default_state_path())
    args = parser.parse_args(argv)
    print(json.dumps(run_update(state_path=args.state_path), sort_keys=True))


def maybe_spawn_auto_update(*, state_path: Path | None = None) -> JsonDict:
    if not auto_update_enabled():
        return {"spawned": False, "reason": "disabled"}
    path = state_path or default_state_path()
    script = Path(__file__).resolve()
    subprocess.Popen(
        [sys.executable, str(script), "--state-path", str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    return {"spawned": True}


def run_update(*, state_path: Path) -> JsonDict:
    if not auto_update_enabled():
        return {"updated": False, "reason": "disabled"}

    state_path = state_path.expanduser().resolve()
    with update_lock(state_path) as acquired:
        if not acquired:
            return {"updated": False, "reason": "locked"}

        now = time.time()
        state = read_state(state_path)
        if not update_due(state, now=now):
            return {"updated": False, "reason": "not_due"}

        marketplace_result = run_claude_command(["plugin", "marketplace", "update", MARKETPLACE_NAME])
        plugin_command = plugin_update_command()
        plugin_result = run_claude_command(plugin_command)
        next_state: JsonDict = {
            "last_checked_at": datetime.now(timezone.utc).isoformat(),
            "last_checked_at_epoch": now,
            "marketplace_exit_code": marketplace_result,
            "plugin_exit_code": plugin_result,
            "plugin_command": plugin_command[1],
        }
        write_state(state_path, next_state)
        return {
            "updated": marketplace_result == 0 and plugin_result == 0,
            "marketplace_exit_code": marketplace_result,
            "plugin_exit_code": plugin_result,
            "plugin_command": plugin_command[1],
        }


def plugin_update_command() -> list[str]:
    if run_claude_command(["plugin", "update", "--help"], timeout=10) == 0:
        return ["plugin", "update", PLUGIN_ID]
    return ["plugin", "install", PLUGIN_ID]


def run_claude_command(arguments: list[str], *, timeout: int = UPDATE_TIMEOUT_SECONDS) -> int | None:
    try:
        result = subprocess.run(
            [claude_cli(), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.returncode


def claude_cli() -> str:
    return os.environ.get(CLAUDE_CLI_ENV) or "claude"


def auto_update_enabled() -> bool:
    return os.environ.get(AUTO_UPDATE_ENV, "1").strip().lower() not in {"0", "false", "no", "off"}


def update_interval_seconds() -> int:
    raw = os.environ.get(AUTO_UPDATE_INTERVAL_ENV)
    if raw is None:
        return DEFAULT_AUTO_UPDATE_INTERVAL_SECONDS
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_AUTO_UPDATE_INTERVAL_SECONDS


def default_state_path() -> Path:
    root = os.environ.get(STATE_DIR_ENV)
    state_dir = Path(root).expanduser() if root else Path.home() / ".claude" / "session-logging"
    return state_dir / "marketplace-update.json"


def read_state(path: Path) -> JsonDict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def write_state(path: Path, value: JsonDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def update_due(state: JsonDict, *, now: float) -> bool:
    previous = state.get("last_checked_at_epoch")
    if not isinstance(previous, (int, float)):
        return True
    return now - previous >= update_interval_seconds()


@contextlib.contextmanager
def update_lock(state_path: Path) -> Iterator[bool]:
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


if __name__ == "__main__":
    main()
