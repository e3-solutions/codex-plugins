#!/usr/bin/env python3
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from session_logging import capture_hook_event, read_stdin_json


def main() -> None:
    try:
        capture_hook_event(read_stdin_json(), event_name="SessionStart")
        spawn_backfill()
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Codex.
        print(f"codex-session-logging capture failed: {exc}", file=sys.stderr)


def spawn_backfill() -> None:
    disabled_values = {"0", "false", "no", "off"}
    if os.environ.get("CODEX_SESSION_LOG_BACKFILL", "1").strip().lower() in disabled_values:
        return
    script = Path(__file__).with_name("backfill_sessions.py")
    default_state_dir = Path.home() / ".codex" / "session-logging"
    state_dir = Path(os.environ.get("CODEX_SESSION_LOG_STATE_DIR", default_state_dir))
    log_path = state_dir / "backfills" / "v1" / "worker.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        subprocess.Popen(
            [sys.executable, str(script)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            start_new_session=True,
        )


if __name__ == "__main__":
    main()
