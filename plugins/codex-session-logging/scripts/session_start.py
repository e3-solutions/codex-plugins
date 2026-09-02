#!/usr/bin/env python3
from __future__ import annotations

import sys

from collective import session_context
from rollout_sync import sync_after_hook
from session_logging import capture_hook_event, read_stdin_json, should_prompt_collective


def main() -> None:
    payload = read_stdin_json()
    try:
        capture_hook_event(payload, event_name="SessionStart")
        sync_after_hook(payload, event_name="SessionStart")
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Codex.
        print(f"codex-session-logging capture failed: {exc}", file=sys.stderr)
    try:
        context = session_context(eligible=should_prompt_collective(payload))
        if context:
            print(context)
    except Exception as exc:  # noqa: BLE001 - guidance must not interrupt Codex.
        print(f"collective context failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
