#!/usr/bin/env python3
from __future__ import annotations

import sys

from session_logging import capture_hook_event, first_string, read_stdin_json


def main() -> None:
    try:
        from update_plugin import maybe_spawn_auto_update

        maybe_spawn_auto_update()
    except Exception:  # noqa: BLE001 - updates must not interrupt Claude Code.
        pass

    payload = read_stdin_json()
    try:
        capture_hook_event(payload, event_name="SessionStart")
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Claude Code.
        print(f"claude-session-logging capture failed: {exc}", file=sys.stderr)

    # Keep an idle-but-open session alive between turns: a detached ticker
    # republishes metadata-only presence while the transcript is fresh and marks
    # the session ended after ~5 minutes idle. Best-effort; never blocks Claude.
    try:
        from presence_ticker import spawn

        session_id = first_string(payload, "session_id", "sessionId") or ""
        transcript_path = first_string(payload, "transcript_path", "transcriptPath") or ""
        spawn(session_id, transcript_path)
    except Exception:  # noqa: BLE001 - presence must not interrupt Claude Code.
        pass


if __name__ == "__main__":
    main()
