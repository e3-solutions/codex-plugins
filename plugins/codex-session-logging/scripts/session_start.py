#!/usr/bin/env python3
from __future__ import annotations

import sys
import os

from collective import session_context
from rollout_sync import sync_after_hook
from session_logging import capture_hook_event, read_stdin_json, should_prompt_collective


def main() -> None:
    payload = read_stdin_json()
    preview = os.environ.get("E3_COLLECTIVE_FEEDBACK_HOOK_ENABLED", "1") == "1"
    if preview:
        try:
            from collective_feedback import feedback_context

            context = feedback_context(payload, event_name="SessionStart", agent="codex")
            if context:
                print(context, flush=True)
        except Exception:
            pass  # An incomplete preview package must not stop telemetry.
    try:
        capture_hook_event(payload, event_name="SessionStart")
        sync_after_hook(payload, event_name="SessionStart")
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Codex.
        print(f"codex-session-logging capture failed: {exc}", file=sys.stderr)
    try:
        context = None if preview else session_context(eligible=should_prompt_collective(payload))
        if context:
            print(context)
    except Exception as exc:  # noqa: BLE001 - guidance must not interrupt Codex.
        print(f"collective context failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
