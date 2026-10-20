#!/usr/bin/env python3
"""PostToolUse / PostToolUseFailure for Sesh tools only: the finished (or failed) event with the
Sesh request ids, arm, plugin version and timing. Metadata only; the result itself is never kept."""
from __future__ import annotations

import sys


def main() -> None:
    try:
        from sesh_cc import is_sesh_tool
        from sesh_linking import (capture, duration_ms, read_stdin_json, search_metadata,
                                  tool_metadata, tool_success)

        payload = read_stdin_json()
        if not is_sesh_tool(payload.get("tool_name")):
            return
        failed = payload.get("hook_event_name") == "PostToolUseFailure"
        if failed:
            from sesh_cc import is_sesh_search_tool, release_search

            if is_sesh_search_tool(payload.get("tool_name")):
                try:
                    release_search(payload)
                except Exception:  # noqa: BLE001
                    pass
        metadata = tool_metadata(payload, "failed" if failed else "finished")
        if not failed:
            metadata.update(search_metadata(payload))
        success = tool_success(payload)
        if success is not None:
            metadata["success"] = success
        elapsed = duration_ms(payload)
        if elapsed is not None:
            metadata["duration_ms"] = elapsed
        capture(payload, event_type="tool_call_failed" if failed else "tool_call_finished",
                hook_event=payload.get("hook_event_name") or "PostToolUse", metadata=metadata)
    except Exception as exc:  # noqa: BLE001 - linking must not interrupt Claude Code.
        print(f"sesh linking failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
