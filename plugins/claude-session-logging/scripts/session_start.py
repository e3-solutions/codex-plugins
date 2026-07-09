#!/usr/bin/env python3
from __future__ import annotations

import sys

from session_logging import capture_hook_event, read_stdin_json


def main() -> None:
    try:
        from update_plugin import maybe_spawn_auto_update

        maybe_spawn_auto_update()
    except Exception:  # noqa: BLE001 - updates must not interrupt Claude Code.
        pass
    try:
        capture_hook_event(read_stdin_json(), event_name="SessionStart")
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Claude Code.
        print(f"claude-session-logging capture failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
