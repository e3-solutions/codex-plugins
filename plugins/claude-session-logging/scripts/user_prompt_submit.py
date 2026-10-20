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
    forum_meta = None
    try:
        from forum_cue import forum_cue_decision

        cue, forum_meta = forum_cue_decision(payload)  # COR-4883 arm only while FORUM_CUE_AB is on.
        if cue:
            print(cue, flush=True)
    except Exception:
        pass  # Forum guidance must not interrupt logging or the user's prompt.
    try:
        if forum_meta:
            capture_hook_event(payload, event_name="UserPromptSubmit", extra_metadata=forum_meta)
        else:
            capture_hook_event(payload, event_name="UserPromptSubmit")
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Claude Code.
        print(f"claude-session-logging capture failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
