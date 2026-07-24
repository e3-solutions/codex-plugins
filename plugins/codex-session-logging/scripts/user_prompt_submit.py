#!/usr/bin/env python3
from __future__ import annotations

import sys

from rollout_sync import sync_after_hook
from session_logging import capture_hook_event, read_stdin_json


def main() -> None:
    try:
        payload = read_stdin_json()
        capture_hook_event(payload, event_name="UserPromptSubmit")
        sync_after_hook(payload, event_name="UserPromptSubmit")
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Codex.
        print(f"codex-session-logging capture failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
