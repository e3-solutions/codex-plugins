#!/usr/bin/env python3
from __future__ import annotations

import json
import sys

from collective import stop_feedback
from session_logging import capture_hook_event, read_stdin_json, should_prompt_collective


def main() -> None:
    payload = read_stdin_json()
    try:
        capture_hook_event(payload, event_name="Stop")
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Claude Code.
        print(f"claude-session-logging capture failed: {exc}", file=sys.stderr)

    # The completed turn (prompt/response/tool bodies + token usage) is now in the
    # transcript; sync it into codex_session_messages / codex_session_usage.
    try:
        import transcript_sync

        transcript_sync.sync_from_hook(payload)
    except Exception as exc:  # noqa: BLE001 - transcript sync must not interrupt Claude Code.
        print(f"claude-session-logging transcript sync failed: {exc}", file=sys.stderr)

    try:
        feedback = stop_feedback(payload, eligible=should_prompt_collective(payload))
        if feedback:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "Stop",
                            "additionalContext": feedback,
                        }
                    }
                )
            )
    except Exception as exc:  # noqa: BLE001 - guidance must not interrupt Claude Code.
        print(f"collective assessment failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
