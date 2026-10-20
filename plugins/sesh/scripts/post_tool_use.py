#!/usr/bin/env python3
"""PostToolUse: Sesh search linking (request ids, origin session, source handles) for Sesh tools only."""
from __future__ import annotations

import sys

from sesh_plugin import is_sesh_tool, linking_enabled, logger_runs
from session_logging import capture_hook_event, read_stdin_json


def main() -> None:
    try:
        payload = read_stdin_json()
        if not is_sesh_tool(payload.get("tool_name")) or logger_runs("PostToolUse"):
            return
        try:
            from sesh_need import record_search_outcome
            from session_logging import tool_success

            record_search_outcome(payload, tool_success(payload))  # COR-4887: for guard denial logs.
        except Exception:  # noqa: BLE001
            pass
        if not linking_enabled():
            return
        try:
            from sesh_hooks import sesh_event_metadata

            extra = sesh_event_metadata(payload)  # COR-4887 arm.
        except Exception:  # noqa: BLE001
            extra = {}
        capture_hook_event(payload, event_name="PostToolUse", extra_metadata=extra or None)
    except Exception as exc:  # noqa: BLE001 - linking must not interrupt Codex.
        print(f"sesh linking failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
