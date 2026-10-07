#!/usr/bin/env python3
from __future__ import annotations

import sys

from session_logging import capture_hook_event, read_stdin_json


def main() -> None:
    try:
        payload = read_stdin_json()
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Codex.
        print(f"codex-session-logging capture failed: {exc}", file=sys.stderr)
        return
    try:
        from sesh_gate import check_sesh_search

        blocked = check_sesh_search(payload)
    except Exception:  # noqa: BLE001 - the Sesh gate must never break a tool call.
        blocked = None
    if blocked:
        print(blocked, file=sys.stderr, flush=True)
        raise SystemExit(2)
    try:
        capture_hook_event(payload, event_name="PreToolUse")
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Codex.
        print(f"codex-session-logging capture failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
