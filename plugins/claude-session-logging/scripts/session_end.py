#!/usr/bin/env python3
from __future__ import annotations

import sys

from session_logging import capture_hook_event, read_stdin_json


def main() -> None:
    payload = read_stdin_json()
    try:
        capture_hook_event(payload, event_name="SessionEnd")
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Claude Code.
        print(f"claude-session-logging capture failed: {exc}", file=sys.stderr)

    # Flush any remaining transcript turns (final response + usage) at session end.
    try:
        import transcript_sync

        transcript_sync.sync_from_hook(payload)
    except Exception as exc:  # noqa: BLE001 - transcript sync must not interrupt Claude Code.
        print(f"claude-session-logging transcript sync failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
