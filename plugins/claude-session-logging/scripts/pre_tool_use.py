#!/usr/bin/env python3
from __future__ import annotations

import sys

from session_logging import capture_hook_event, read_stdin_json


def forum_decision(payload):
    try:
        from forum_decision_cue import decision_cue

        return decision_cue(payload)  # COR-4911, on unless FORUM_DECISION_CUE=0/false/no/off.
    except Exception:  # noqa: BLE001 - the cue fails open.
        return None, None


def main() -> None:
    try:
        payload = read_stdin_json()
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Claude Code.
        print(f"claude-session-logging capture failed: {exc}", file=sys.stderr)
        return
    output, event = forum_decision(payload)
    try:
        if event:
            from session_logging import capture_metadata_event, should_capture_payload

            if should_capture_payload(payload):
                capture_metadata_event(payload, hook_event="PreToolUse", event_type=event["event_type"],
                                       event_metadata=event["metadata"])
        capture_hook_event(payload, event_name="PreToolUse")
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Claude Code.
        print(f"claude-session-logging capture failed: {exc}", file=sys.stderr)
    try:
        if output:
            print(output, flush=True)
    except Exception:  # noqa: BLE001 - never break the tool call.
        pass


if __name__ == "__main__":
    main()
