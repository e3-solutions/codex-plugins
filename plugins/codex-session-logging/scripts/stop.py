#!/usr/bin/env python3
from __future__ import annotations

import json
import sys

from collective import stop_feedback
from rollout_sync import sync_after_hook
from session_logging import capture_hook_event, read_stdin_json, should_prompt_collective


def main() -> None:
    payload = read_stdin_json()
    try:
        capture_hook_event(payload, event_name="Stop")
        sync_after_hook(payload, event_name="Stop")
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Codex.
        print(f"codex-session-logging capture failed: {exc}", file=sys.stderr)
    try:
        feedback = stop_feedback(payload, eligible=should_prompt_collective(payload))
        if feedback:
            print(json.dumps({"decision": "block", "reason": feedback}))
    except Exception as exc:  # noqa: BLE001 - guidance must not interrupt Codex.
        print(f"collective assessment failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
