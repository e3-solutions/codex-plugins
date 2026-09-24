#!/usr/bin/env python3
from __future__ import annotations

import sys

from session_logging import capture_hook_event, read_stdin_json


def main() -> None:
    try:
        payload = read_stdin_json()
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Claude Code.
        print(f"claude-session-logging capture failed: {exc}", file=sys.stderr)
        return
    try:
        from forum_cue import forum_cue

        cue = forum_cue(payload)
        if cue:
            print(cue, flush=True)
    except Exception:
        pass  # Forum guidance must not interrupt logging or the user's prompt.
    try:
        capture_hook_event(payload, event_name="UserPromptSubmit")
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Claude Code.
        print(f"claude-session-logging capture failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
