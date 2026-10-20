#!/usr/bin/env python3
from __future__ import annotations

import sys

from rollout_sync import sync_after_hook
from session_logging import capture_hook_event, read_stdin_json


def main() -> None:
    try:
        payload = read_stdin_json()
        extra = {}
        try:
            from sesh_hooks import sesh_event_metadata

            extra = sesh_event_metadata(payload)  # COR-4887 arm on Sesh tool events only.
        except Exception:  # noqa: BLE001
            extra = {}
        try:
            from sesh_need import record_search_outcome
            from session_logging import tool_success

            record_search_outcome(payload, tool_success(payload))  # COR-4887: for guard denial logs.
        except Exception:  # noqa: BLE001
            pass
        capture_hook_event(payload, event_name="PostToolUse", extra_metadata=extra or None)
        sync_after_hook(payload, event_name="PostToolUse")
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Codex.
        print(f"codex-session-logging capture failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
